from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterable
from typing import Any

from assistant.contracts import RequestResultItem, SkillResult
from assistant.logic.contracts import (
    ChoiceFilter,
    ChoiceOption,
    LogicPredicate,
    LogicSelection,
)


def _nested(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _choice_value(option: ChoiceOption, path: str) -> Any:
    if path == "label":
        return option.label
    if path == "value":
        return option.value
    if path == "metadata":
        return option.metadata
    if path.startswith("metadata."):
        return _nested(option.metadata, path.removeprefix("metadata."))
    return None


def _matches_filter(option: ChoiceOption, item: ChoiceFilter) -> bool:
    actual = _choice_value(option, item.path)
    if item.operator == "equals":
        return actual == item.value
    if item.operator == "not_equals":
        return actual != item.value
    if item.operator == "contains":
        if isinstance(actual, str) and isinstance(item.value, str):
            return item.value.casefold() in actual.casefold()
        if isinstance(actual, (list, tuple, set, dict)):
            return item.value in actual
        return False
    if item.operator == "in":
        return isinstance(item.value, list) and actual in item.value
    return False


def options_from_items(items: tuple[RequestResultItem, ...] | None) -> tuple[ChoiceOption, ...]:
    return tuple(
        ChoiceOption(
            option_id=item.item_id,
            label=item.title,
            value=item.value,
            metadata=item.metadata,
        )
        for item in items or ()
        if not item.sensitive
    )


def eligible_options(
    selection: LogicSelection,
    dynamic_options: tuple[ChoiceOption, ...] = (),
) -> tuple[ChoiceOption, ...]:
    options = selection.options if selection.source == "static" else dynamic_options
    return tuple(
        option
        for option in options
        if all(_matches_filter(option, item) for item in selection.filters)
    )


def _deterministic_unit(selection_key: str, seed: str | None) -> float:
    if seed is None:
        return secrets.randbits(64) / float(2**64)
    payload = json.dumps([seed or "", selection_key], separators=(",", ":"))
    value = int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")
    return value / float(2**64)


def choose_option(
    selection: LogicSelection,
    *,
    selection_key: str,
    recent_ids: Iterable[str] = (),
    dynamic_options: tuple[ChoiceOption, ...] = (),
) -> ChoiceOption:
    options = eligible_options(selection, dynamic_options)
    if not options:
        raise ValueError("No eligible choices remain after filtering")
    recent = tuple(recent_ids)
    recent_window = set(recent[: selection.avoid_recent])
    candidates = tuple(option for option in options if option.option_id not in recent_window)
    if not candidates:
        candidates = options

    if selection.strategy == "round_robin":
        if recent:
            by_id = {option.option_id: index for index, option in enumerate(options)}
            if recent[0] in by_id:
                return options[(by_id[recent[0]] + 1) % len(options)]
        return options[0]

    if selection.strategy == "least_recent":
        positions = {option_id: index for index, option_id in enumerate(recent)}
        never_used = tuple(option for option in candidates if option.option_id not in positions)
        if never_used:
            candidates = never_used
        else:
            farthest = max(positions.get(option.option_id, len(recent)) for option in candidates)
            candidates = tuple(
                option
                for option in candidates
                if positions.get(option.option_id, len(recent)) == farthest
            )

    total = sum(option.weight for option in candidates)
    target = _deterministic_unit(selection_key, selection.seed) * total
    cursor = 0.0
    for option in candidates:
        cursor += option.weight
        if target < cursor:
            return option
    return candidates[-1]


def resolve_predicate_reference(predicate: LogicPredicate, result: SkillResult) -> Any:
    reference = predicate.reference
    if reference == "check.status":
        return result.status.value
    if reference == "check.summary":
        return result.summary
    if reference == "check.items_count":
        return len(result.items or ())
    if reference == "check.data":
        return result.data
    if reference.startswith("check.data."):
        return _nested(result.data, reference.removeprefix("check.data."))
    return None


def evaluate_predicate(predicate: LogicPredicate, result: SkillResult) -> bool:
    actual = resolve_predicate_reference(predicate, result)
    expected = predicate.value
    if predicate.operator == "exists":
        return actual is not None
    if predicate.operator == "equals":
        return actual == expected
    if predicate.operator == "not_equals":
        return actual != expected
    if predicate.operator == "contains":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected.casefold() in actual.casefold()
        if isinstance(actual, (list, tuple, set, dict)):
            return expected in actual
        return False
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        return False
    if not isinstance(expected, (int, float)) or isinstance(expected, bool):
        return False
    return {
        "less_than": actual < expected,
        "less_or_equal": actual <= expected,
        "greater_than": actual > expected,
        "greater_or_equal": actual >= expected,
    }.get(predicate.operator, False)


def render_template(
    template: str,
    *,
    choice: ChoiceOption | None,
    check: SkillResult | None,
    max_length: int,
) -> str:
    values = {
        "choice.id": choice.option_id if choice else "",
        "choice.label": choice.label if choice else "",
        "choice.value": str(choice.value) if choice else "",
        "check.status": check.status.value if check else "",
        "check.summary": check.summary if check else "",
        "check.items_count": str(len(check.items or ())) if check else "0",
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered[:max_length]
