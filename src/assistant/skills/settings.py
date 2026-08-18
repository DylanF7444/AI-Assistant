from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.settings_service import ApplicationSettingsService
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest


class AssistantSettingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["get", "set", "toggle"]
    setting: str = Field(
        min_length=1,
        max_length=100,
        description=(
            "Allowlisted assistant setting name, such as response_detail, speech_mode, "
            "ui_high_contrast, offline_lock, or background_actions_enabled."
        ),
    )
    value: Any = None

    @model_validator(mode="after")
    def set_requires_value(self) -> AssistantSettingInput:
        if self.operation == "set" and "value" not in self.model_fields_set:
            raise ValueError("set requires value")
        if self.operation != "set" and "value" in self.model_fields_set:
            raise ValueError(f"value is not valid for {self.operation}")
        return self


class AssistantSettingOutput(BaseModel):
    setting: str
    value: Any
    previous_value: Any
    requires_confirmation: bool
    restart_required: bool


class AssistantSettingSkill(Skill):
    input_model = AssistantSettingInput
    output_model = AssistantSettingOutput

    def __init__(self, service: ApplicationSettingsService) -> None:
        self.service = service
        self.manifest = manifest(
            skill_id="core.settings",
            display_name="Assistant Settings",
            description=(
                "Read or change an allowlisted assistant application setting. Supports response "
                "detail, speech and voice activation, accessibility, notifications, privacy "
                "context, offline lock, local/cloud model choices, and scheduling settings."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="settings.get",
                    targets=("*",),
                    reason="Read an assistant setting.",
                ),
                PermissionScope(
                    capability="settings.set",
                    targets=("*",),
                    reason="Change an assistant setting.",
                ),
            ),
            risk=RiskLevel.HIGH,
            background_mode=BackgroundMode.FOREGROUND_ONLY,
        )

    def required_permissions(self, call: SkillCall) -> tuple[PermissionScope, ...]:
        args = self.validate(call.arguments)
        definition = self.service.get(args.setting).setting
        reading = args.operation == "get"
        return (
            PermissionScope(
                capability="settings.get" if reading else "settings.set",
                targets=(definition.name,),
                reason=(
                    f"Read the {definition.label} assistant setting."
                    if reading
                    else f"Change the {definition.label} assistant setting."
                ),
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        args = self.validate(call.arguments)
        if args.operation == "get":
            return RiskLevel.LOW
        return self.service.get(args.setting).setting.risk

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "get":
            definition = self.service.get(args.setting).setting
            return f"Read assistant setting {definition.label}"
        change = self.service.preview(
            args.setting,
            args.value,
            toggle=args.operation == "toggle",
        )
        return f"Change assistant setting {change.setting.label} to {change.value!r}"

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(
                status=ResultStatus.CANCELLED,
                summary="The settings request was cancelled.",
            )
        if args.operation == "get":
            change = self.service.get(args.setting)
            summary = f"Assistant setting {change.setting.label} is {change.value!r}."
        else:
            change = self.service.update(
                args.setting,
                args.value,
                toggle=args.operation == "toggle",
            )
            restart = (
                " Restart the assistant for this setting to take effect."
                if (change.setting.restart_required)
                else ""
            )
            summary = (
                f"Changed assistant setting {change.setting.label} to {change.value!r}.{restart}"
            )
        output = AssistantSettingOutput(
            setting=change.setting.name,
            value=change.value,
            previous_value=change.previous_value,
            requires_confirmation=change.setting.requires_confirmation,
            restart_required=change.setting.restart_required,
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            data=output.model_dump(mode="json"),
        )
