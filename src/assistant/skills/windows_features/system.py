from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from assistant.contracts import (
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.windows.interfaces import (
    InputAutomationAdapter,
    NotificationAdapter,
    ProcessAdapter,
    WindowsSettingsAdapter,
)


class NotificationInput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)


class NotificationOutput(BaseModel):
    shown: bool


class NotificationSkill(Skill):
    input_model = NotificationInput
    output_model = NotificationOutput

    def __init__(self, adapter: NotificationAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.notification",
            display_name="Notification",
            description="Show a visible local notification.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="notification.create",
                    reason="Display the requested local notification.",
                ),
            ),
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        await self.adapter.show(args.title, args.message)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Notification displayed.",
            data=NotificationOutput(shown=True).model_dump(),
        )


class ProcessInput(BaseModel):
    operation: Literal["list", "terminate"]
    name_filter: str | None = None
    pid: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def pid_required(self):
        if self.operation == "terminate" and self.pid is None:
            raise ValueError("terminate requires a PID")
        return self


class ProcessOutput(BaseModel):
    processes: list[dict]
    terminated_pid: int | None = None


class ProcessSkill(Skill):
    input_model = ProcessInput
    output_model = ProcessOutput

    def __init__(self, adapter: ProcessAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.process",
            display_name="Process",
            description="Inspect or terminate an exact Windows process.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.CRITICAL,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        target = str(args.pid) if args.pid else (args.name_filter or "*")
        return (
            PermissionScope(
                capability=f"process.{args.operation}",
                targets=(target,),
                reason=f"{args.operation.title()} the exact displayed process target.",
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.CRITICAL
            if self.validate(call.arguments).operation == "terminate"
            else RiskLevel.MEDIUM
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "terminate":
            return f"Terminate process PID {args.pid}"
        return f"List processes matching {args.name_filter or 'all processes'}"

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if args.operation == "list":
            rows = await self.adapter.list(args.name_filter)
            processes = [
                {
                    "image_name": row.image_name,
                    "pid": row.pid,
                    "session_name": row.session_name,
                    "memory": row.memory,
                }
                for row in rows
            ]
            return SkillResult(
                status=ResultStatus.COMPLETED,
                summary=f"Found {len(processes)} processes.",
                data=ProcessOutput(processes=processes).model_dump(),
                sensitive=True,
            )
        assert args.pid
        if not await self.adapter.terminate(args.pid):
            raise RuntimeError(f"Could not terminate PID {args.pid}")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Terminated process PID {args.pid}.",
            data=ProcessOutput(processes=[], terminated_pid=args.pid).model_dump(),
        )


class InputAutomationInput(BaseModel):
    operation: Literal["type_text", "click"]
    application: str = Field(pattern=r"^[^\\/:*?\"<>|]+\.exe$")
    text: str | None = Field(default=None, max_length=2000)
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def operation_arguments(self):
        if self.operation == "type_text" and self.text is None:
            raise ValueError("type_text requires text")
        if self.operation == "click" and (self.x is None or self.y is None):
            raise ValueError("click requires x and y")
        return self


class InputAutomationOutput(BaseModel):
    operation: str
    application: str


class InputAutomationSkill(Skill):
    input_model = InputAutomationInput
    output_model = InputAutomationOutput

    def __init__(self, adapter: InputAutomationAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.input_automation",
            display_name="Input Automation",
            description="Visible keyboard or mouse automation in one approved application.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.HIGH,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return (
            PermissionScope(
                capability="input.control",
                targets=(args.application.casefold(),),
                reason="Send visible input only to this exact approved application.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "type_text":
            return (
                f"Type {len(args.text or '')} characters into "
                f"{args.application}; text is hidden for privacy"
            )
        return f"Click ({args.x}, {args.y}) in {args.application}"

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        active = await self.adapter.active_process_name()
        if active is None or active.casefold() != args.application.casefold():
            raise PermissionError(
                f"Active application is {active or 'unknown'}, not {args.application}"
            )
        if args.operation == "type_text":
            if await self.adapter.focused_control_is_sensitive():
                raise PermissionError(
                    "Typing is blocked because the focused control may contain sensitive data"
                )
            await self.adapter.type_text(args.text or "")
        else:
            await self.adapter.click(args.x, args.y)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Input automation completed in {args.application}.",
            data=InputAutomationOutput(
                operation=args.operation, application=args.application
            ).model_dump(),
            sensitive=args.operation == "type_text",
        )


class WindowsSettingInput(BaseModel):
    operation: Literal["read", "set", "toggle"]
    setting: Literal["dark_mode"]
    enabled: bool | None = None

    @model_validator(mode="after")
    def value_required(self):
        if self.operation == "set" and self.enabled is None:
            raise ValueError("set requires enabled")
        return self


class WindowsSettingOutput(BaseModel):
    setting: str
    enabled: bool


class WindowsSettingSkill(Skill):
    input_model = WindowsSettingInput
    output_model = WindowsSettingOutput

    def __init__(self, adapter: WindowsSettingsAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.setting",
            display_name="Windows Setting",
            description="Read or change an explicitly supported Windows setting.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.MEDIUM,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        operation = "read" if args.operation == "read" else "set"
        return (
            PermissionScope(
                capability=f"settings.{operation}",
                targets=(args.setting,),
                reason=f"{args.operation.title()} the approved Windows setting.",
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.MEDIUM if self.validate(call.arguments).operation == "set" else RiskLevel.LOW
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        return (
            "Read Windows dark mode"
            if args.operation == "read"
            else (
                "Toggle Windows dark mode"
                if args.operation == "toggle"
                else f"Set Windows dark mode to {args.enabled}"
            )
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if args.operation in {"set", "toggle"}:
            enabled = (
                not await self.adapter.get_dark_mode()
                if args.operation == "toggle"
                else bool(args.enabled)
            )
            await self.adapter.set_dark_mode(enabled)
        enabled = await self.adapter.get_dark_mode()
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Windows dark mode is {'on' if enabled else 'off'}.",
            data=WindowsSettingOutput(setting=args.setting, enabled=enabled).model_dump(),
        )
