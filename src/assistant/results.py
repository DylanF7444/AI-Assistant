from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from assistant.contracts import RequestResultItem

SelectionFailure = Literal["no_results", "out_of_range", "not_found", "ambiguous"]

_RESULT_NOUN = (
    r"(?:one|item|result|match|article|headline|story|link|file|task|option)"
)
_RESULT_ORDINAL = (
    r"(?:first|one|second|two|third|three|fourth|four|fifth|five|last|"
    r"\d+(?:st|nd|rd|th)?)"
)


@dataclass(frozen=True)
class ResultItemSelection:
    item: RequestResultItem | None = None
    failure: SelectionFailure | None = None


def selection_index(selector: str) -> int | None:
    """Return a zero-based ordinal from common spoken or typed selectors."""

    value = selector.strip().casefold()
    value = re.sub(r"^(?:the|that|this)\s+", "", value)
    value = re.sub(rf"\s+{_RESULT_NOUN}$", "", value).strip()
    value = re.sub(
        rf"^(?:{_RESULT_NOUN}|number)\s+(?:number\s+)?",
        "",
        value,
    ).strip()
    words = {
        "first": 1,
        "one": 1,
        "second": 2,
        "two": 2,
        "third": 3,
        "three": 3,
        "fourth": 4,
        "four": 4,
        "fifth": 5,
        "five": 5,
        "last": -1,
    }
    if value in words:
        number = words[value]
        return number - 1 if number > 0 else -1
    if match := re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", value):
        return int(match.group(1)) - 1
    return None


def contextual_result_selector(text: str) -> str | None:
    """Extract an explicit reference to an item in the latest result set.

    This intentionally requires a result-like noun so an unrelated question such
    as "Who was the first president?" cannot silently select stale results.
    """

    normalized = re.sub(r"[^a-z0-9#'’.-]+", " ", text.casefold()).strip()
    if not normalized:
        return None
    patterns = (
        rf"\b(?:the\s+)?(?P<selector>{_RESULT_ORDINAL})\s+{_RESULT_NOUN}\b",
        rf"\b{_RESULT_NOUN}\s+(?:number\s+)?(?P<selector>{_RESULT_ORDINAL})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return match.group("selector")
    return None


def select_result_item(
    items: tuple[RequestResultItem, ...],
    selector: str,
) -> ResultItemSelection:
    """Resolve an ordinal, item ID, or unambiguous title in the latest result set."""

    if not items:
        return ResultItemSelection(failure="no_results")
    cleaned = selector.strip().strip("\"'").casefold()
    index = selection_index(cleaned)
    if index == -1:
        index = len(items) - 1
    if index is not None:
        if 0 <= index < len(items):
            return ResultItemSelection(item=items[index])
        return ResultItemSelection(failure="out_of_range")

    normalized = re.sub(
        r"^(?:the\s+)?(?:item|result|match)\s+(?:named|called|titled)\s+",
        "",
        cleaned,
    )
    normalized = normalized.removeprefix("#")
    id_matches = [item for item in items if item.item_id.casefold() == normalized]
    if len(id_matches) == 1:
        return ResultItemSelection(item=id_matches[0])

    exact_title_matches = [item for item in items if item.title.casefold() == normalized]
    if len(exact_title_matches) == 1:
        return ResultItemSelection(item=exact_title_matches[0])
    if len(exact_title_matches) > 1:
        return ResultItemSelection(failure="ambiguous")

    title_matches = [item for item in items if normalized and normalized in item.title.casefold()]
    if len(title_matches) == 1:
        return ResultItemSelection(item=title_matches[0])
    if len(title_matches) > 1:
        return ResultItemSelection(failure="ambiguous")
    return ResultItemSelection(failure="not_found")


def result_item_context(
    item: RequestResultItem,
    *,
    position: int,
) -> dict[str, object]:
    """Return the bounded, explicit representation exposed to assistant context."""

    if item.sensitive:
        title = "[sensitive item]"
        value: object = "[redacted]"
        description = None
        metadata: dict[str, object] = {}
    else:
        title = item.title
        value = _bounded_context_value(item.value)
        description = item.description
        metadata = _bounded_context_value(item.metadata)
    return {
        "position": position,
        "reference": f"item {position}",
        "item_id": item.item_id,
        "kind": item.kind,
        "title": title,
        "value": value,
        "description": description,
        "metadata": metadata,
    }


def _bounded_context_value(value: object, *, depth: int = 0) -> object:
    if depth >= 3:
        return str(value)[:500]
    if isinstance(value, str):
        return value[:2_000]
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, dict):
        return {
            str(key)[:100]: _bounded_context_value(item, depth=depth + 1)
            for key, item in list(value.items())[:20]
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_context_value(item, depth=depth + 1)
            for item in value[:20]
        ]
    return str(value)[:2_000]
