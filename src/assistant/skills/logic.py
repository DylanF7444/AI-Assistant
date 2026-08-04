from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue, model_validator

from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    RequestResultItem,
    ResultStatus,
    SkillCall,
    SkillResult,
)
from assistant.logic.contracts import ChoiceFilter, ChoiceOption, LogicSelection
from assistant.logic.evaluator import choose_option, eligible_options
from assistant.logic.store import LogicStateStore
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.windows.interfaces import NotificationAdapter


class ChooseObjectInput(BaseModel):
    option_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    label: str = Field(min_length=1, max_length=300)
    value: JsonValue | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    weight: float = Field(default=1, gt=0, le=1_000)


ChooseInputItem = Annotated[str | ChooseObjectInput, Field(union_mode="left_to_right")]


class ChooseInput(BaseModel):
    items: tuple[ChooseInputItem, ...] = Field(min_length=2, max_length=100)
    strategy: Literal["random", "round_robin", "least_recent"] = "random"
    filters: tuple[ChoiceFilter, ...] = ()
    avoid_recent: int = Field(default=0, ge=0, le=50)
    seed: str | None = Field(default=None, max_length=200)
    history_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )

    @model_validator(mode="after")
    def _ids_are_unique(self) -> ChooseInput:
        ids = [
            item.option_id or f"choice-{index}"
            if isinstance(item, ChooseObjectInput)
            else f"choice-{index}"
            for index, item in enumerate(self.items, 1)
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("Choice IDs must be unique")
        return self


class ChooseOutput(BaseModel):
    selected_id: str
    selected_label: str
    selected_value: JsonValue
    strategy: str
    candidate_count: int
    history_key: str | None = None


class ChooseSkill(Skill):
    input_model = ChooseInput
    output_model = ChooseOutput

    def __init__(self, state: LogicStateStore) -> None:
        self.state = state
        self.manifest = manifest(
            skill_id="core.choose",
            display_name="Choose",
            description=(
                "Choose one item at random, with a repeatable seed, in rotation, or by "
                "least-recent use, with optional filtering and bounded choice history."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            safe_to_retry=True,
            background_mode=BackgroundMode.SAFE,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="Choice cancelled.")
        options = tuple(self._option(item, index) for index, item in enumerate(args.items, 1))
        logical_history_key = args.history_key or self._automatic_history_key(args, options)
        history_key = (
            f"{call.permission_context.user_id}:{logical_history_key}"
            if logical_history_key
            else None
        )
        checkpoint_key = f"choose:{call.idempotency_key}"
        selection = LogicSelection(
            options=options,
            strategy=args.strategy,
            filters=args.filters,
            avoid_recent=args.avoid_recent,
            seed=args.seed,
            history_key=logical_history_key,
        )
        selected_id = self.state.checkpoint_choice(checkpoint_key)
        selected = next((item for item in options if item.option_id == selected_id), None)
        if selected is None:
            selected = choose_option(
                selection,
                selection_key=checkpoint_key,
                recent_ids=self.state.recent_choices(history_key),
            )
            reserved = self.state.reserve_choice(
                checkpoint_key=checkpoint_key,
                option_id=selected.option_id,
                history_key=history_key,
            )
            selected = next(item for item in options if item.option_id == reserved)
        data = ChooseOutput(
            selected_id=selected.option_id,
            selected_label=selected.label,
            selected_value=selected.value,
            strategy=args.strategy,
            candidate_count=len(eligible_options(selection)),
            history_key=logical_history_key,
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"I chose {selected.label}.",
            display_text=f"Selected: {selected.label}",
            data=data.model_dump(mode="json"),
            items=(
                RequestResultItem(
                    item_id=selected.option_id,
                    title=selected.label,
                    value=selected.value,
                    kind="choice",
                    metadata=selected.metadata,
                ),
            ),
        )

    @staticmethod
    def _option(item: ChooseInputItem, index: int) -> ChoiceOption:
        if isinstance(item, str):
            return ChoiceOption(option_id=f"choice-{index}", label=item, value=item)
        return ChoiceOption(
            option_id=item.option_id or f"choice-{index}",
            label=item.label,
            value=item.label if item.value is None else item.value,
            metadata=item.metadata,
            weight=item.weight,
        )

    @staticmethod
    def _automatic_history_key(
        args: ChooseInput,
        options: tuple[ChoiceOption, ...],
    ) -> str | None:
        if args.strategy == "random" and args.avoid_recent == 0:
            return None
        payload = json.dumps(
            {
                "options": [
                    {"id": option.option_id, "label": option.label, "value": option.value}
                    for option in options
                ],
                "filters": [item.model_dump(mode="json") for item in args.filters],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"auto-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


class ChanceInput(BaseModel):
    probability_percent: float = Field(ge=0, le=100)
    title: str = Field(default="Surprise", min_length=1, max_length=100)
    message: str = Field(default="Surprise!", min_length=1, max_length=500)


class ChanceOutput(BaseModel):
    triggered: bool
    probability_percent: float


class ChanceSkill(Skill):
    input_model = ChanceInput
    output_model = ChanceOutput

    def __init__(self, state: LogicStateStore, notifications: NotificationAdapter) -> None:
        self.state = state
        self.notifications = notifications
        self.manifest = manifest(
            skill_id="core.chance",
            display_name="Chance",
            description=(
                "Roll an immediate percentage chance and show one local notification only "
                "when the chance triggers; the non-triggering outcome intentionally does nothing."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="notification.create",
                    reason="Show the requested notification when the chance triggers.",
                ),
            ),
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="Chance roll cancelled.")
        checkpoint_key = f"chance:{call.idempotency_key}"
        selected = self.state.checkpoint_choice(checkpoint_key)
        if selected not in {"trigger", "skip"}:
            if args.probability_percent <= 0:
                selected = "skip"
            elif args.probability_percent >= 100:
                selected = "trigger"
            else:
                selection = LogicSelection(
                    options=(
                        ChoiceOption(
                            option_id="trigger",
                            label="Trigger",
                            value=True,
                            weight=args.probability_percent,
                        ),
                        ChoiceOption(
                            option_id="skip",
                            label="Do nothing",
                            value=False,
                            weight=100 - args.probability_percent,
                        ),
                    )
                )
                selected = choose_option(
                    selection,
                    selection_key=checkpoint_key,
                ).option_id
            selected = self.state.reserve_choice(
                checkpoint_key=checkpoint_key,
                option_id=selected,
                history_key=None,
            )
        triggered = selected == "trigger"
        if triggered:
            await self.notifications.show(args.title, args.message)
        data = ChanceOutput(
            triggered=triggered,
            probability_percent=args.probability_percent,
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=(
                f"The {args.probability_percent:g}% chance triggered; notification shown."
                if triggered
                else f"The {args.probability_percent:g}% chance did not trigger; no action ran."
            ),
            display_text=(
                f"Chance triggered: {args.title}"
                if triggered
                else "Chance did not trigger; no notification was shown."
            ),
            data=data.model_dump(mode="json"),
        )
