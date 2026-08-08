from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from assistant.contracts import SkillCall, StrictModel

_TEMPLATE_FIELD = re.compile(
    r"\{(?:choice\.(?:id|label|value)|check\.(?:status|summary|items_count))\}"
)


class LogicCallDraft(StrictModel):
    skill_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChoiceFilter(StrictModel):
    path: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^(?:label|value|metadata(?:\.[A-Za-z0-9_.-]+)?)$",
    )
    operator: Literal["equals", "not_equals", "contains", "in"] = "equals"
    value: JsonValue


class ChoiceOptionDraft(StrictModel):
    option_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    label: str = Field(min_length=1, max_length=300)
    value: JsonValue
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    weight: float = Field(default=1, gt=0, le=1_000)
    action: LogicCallDraft | None = None


class ChoiceOption(StrictModel):
    option_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    label: str = Field(min_length=1, max_length=300)
    value: JsonValue
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    weight: float = Field(default=1, gt=0, le=1_000)
    action: SkillCall | None = None


class LogicSelectionDraft(StrictModel):
    source: Literal["static", "check_items"] = "static"
    strategy: Literal["random", "round_robin", "least_recent"] = "random"
    options: tuple[ChoiceOptionDraft, ...] = Field(default=(), max_length=100)
    filters: tuple[ChoiceFilter, ...] = Field(default=(), max_length=20)
    avoid_recent: int = Field(default=0, ge=0, le=50)
    seed: str | None = Field(default=None, max_length=200)
    history_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )

    @model_validator(mode="after")
    def _source_has_options(self) -> LogicSelectionDraft:
        if self.source == "static" and len(self.options) < 2:
            raise ValueError("Static selection requires at least two options")
        if self.source == "check_items" and self.options:
            raise ValueError("check_items selection cannot include static options")
        ids = [option.option_id for option in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("Selection option IDs must be unique")
        return self


class LogicSelection(StrictModel):
    source: Literal["static", "check_items"] = "static"
    strategy: Literal["random", "round_robin", "least_recent"] = "random"
    options: tuple[ChoiceOption, ...] = Field(default=(), max_length=100)
    filters: tuple[ChoiceFilter, ...] = Field(default=(), max_length=20)
    avoid_recent: int = Field(default=0, ge=0, le=50)
    seed: str | None = Field(default=None, max_length=200)
    history_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )

    @model_validator(mode="after")
    def _source_has_options(self) -> LogicSelection:
        if self.source == "static" and len(self.options) < 2:
            raise ValueError("Static selection requires at least two options")
        if self.source == "check_items" and self.options:
            raise ValueError("check_items selection cannot include static options")
        ids = [option.option_id for option in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("Selection option IDs must be unique")
        return self


class LogicPredicate(StrictModel):
    reference: str = Field(
        min_length=1,
        max_length=200,
        pattern=(r"^check\.(?:status|summary|items_count|data(?:\.[A-Za-z0-9_.-]+)?)$"),
    )
    operator: Literal[
        "exists",
        "equals",
        "not_equals",
        "less_than",
        "less_or_equal",
        "greater_than",
        "greater_or_equal",
        "contains",
    ]
    value: JsonValue | None = None

    @model_validator(mode="after")
    def _operator_has_value(self) -> LogicPredicate:
        if self.operator != "exists" and self.value is None:
            raise ValueError(f"{self.operator} predicate requires a comparison value")
        return self


class LogicNotificationDraft(StrictModel):
    kind: Literal["notification"] = "notification"
    title: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)

    @field_validator("title", "message")
    @classmethod
    def _templates_are_bounded(cls, value: str) -> str:
        remainder = _TEMPLATE_FIELD.sub("", value)
        if "{" in remainder or "}" in remainder:
            raise ValueError("Unsupported notification template field")
        return value


class LogicSkillActionDraft(StrictModel):
    kind: Literal["skill"] = "skill"
    call: LogicCallDraft


class LogicSelectionResultDraft(StrictModel):
    kind: Literal["selection_result"] = "selection_result"


LogicActionDraft = Annotated[
    LogicNotificationDraft | LogicSkillActionDraft | LogicSelectionResultDraft,
    Field(discriminator="kind"),
]


class LogicNotificationAction(StrictModel):
    kind: Literal["notification"] = "notification"
    title: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)


class LogicSkillAction(StrictModel):
    kind: Literal["skill"] = "skill"
    call: SkillCall


class LogicSelectionResultAction(StrictModel):
    kind: Literal["selection_result"] = "selection_result"


LogicAction = Annotated[
    LogicNotificationAction | LogicSkillAction | LogicSelectionResultAction,
    Field(discriminator="kind"),
]


class CompletionPolicy(StrictModel):
    stop_after_condition: bool = False
    stop_after_successes: int | None = Field(default=None, ge=1, le=10_000)
    max_runs: int = Field(default=100, ge=1, le=10_000)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _expiry_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expires_at must include a time zone")
        return value


class LogicWorkflowDraft(StrictModel):
    check: LogicCallDraft | None = None
    when: LogicPredicate | None = None
    selection: LogicSelectionDraft | None = None
    action: LogicActionDraft
    completion: CompletionPolicy = Field(default_factory=CompletionPolicy)

    @model_validator(mode="after")
    def _parts_are_consistent(self) -> LogicWorkflowDraft:
        if self.when is not None and self.check is None:
            raise ValueError("A condition requires a check call")
        if self.selection is not None and self.selection.source == "check_items" and not self.check:
            raise ValueError("check_items selection requires a check call")
        if self.action.kind == "selection_result" and self.selection is None:
            raise ValueError("selection_result action requires a selection")
        if self.completion.stop_after_condition and self.when is None:
            raise ValueError("stop_after_condition requires a condition")
        if (
            self.selection is not None
            and any(option.action is not None for option in self.selection.options)
            and self.action.kind != "selection_result"
        ):
            raise ValueError(
                "Choice-specific actions require selection_result as the common action"
            )
        return self


class LogicWorkflow(StrictModel):
    check: SkillCall | None = None
    when: LogicPredicate | None = None
    selection: LogicSelection | None = None
    action: LogicAction
    completion: CompletionPolicy = Field(default_factory=CompletionPolicy)

    @model_validator(mode="after")
    def _parts_are_consistent(self) -> LogicWorkflow:
        if self.when is not None and self.check is None:
            raise ValueError("A condition requires a check call")
        if self.selection is not None and self.selection.source == "check_items" and not self.check:
            raise ValueError("check_items selection requires a check call")
        if self.action.kind == "selection_result" and self.selection is None:
            raise ValueError("selection_result action requires a selection")
        if self.completion.stop_after_condition and self.when is None:
            raise ValueError("stop_after_condition requires a condition")
        if (
            self.selection is not None
            and any(option.action is not None for option in self.selection.options)
            and self.action.kind != "selection_result"
        ):
            raise ValueError(
                "Choice-specific actions require selection_result as the common action"
            )
        return self


def workflow_calls(workflow: LogicWorkflow) -> tuple[tuple[str, SkillCall], ...]:
    calls: list[tuple[str, SkillCall]] = []
    if workflow.check is not None:
        calls.append(("logic-check", workflow.check))
    if workflow.action.kind == "skill":
        calls.append(("logic-action", workflow.action.call))
    if workflow.selection is not None:
        calls.extend(
            (f"logic-choice-{option.option_id}", option.action)
            for option in workflow.selection.options
            if option.action is not None
        )
    return tuple(calls)
