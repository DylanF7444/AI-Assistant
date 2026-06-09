from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
from assistant.windows.adapters import (
    ApplicationAdapter,
    FileSystemAdapter,
    NotificationAdapter,
    ProcessAdapter,
    InputAutomationAdapter,
    WindowsSettingsAdapter,
)


class FileOperationInput(BaseModel):
    operation: Literal["copy", "move", "rename", "delete", "open"]
    source: str
    destination: str | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def destination_required(self):
        if self.operation in {"copy", "move", "rename"} and not self.destination:
            raise ValueError(f"{self.operation} requires a destination")
        return self


class FileOperationOutput(BaseModel):
    operation: str
    source: str
    destination: str | None = None
    dry_run: bool


class FileOperationSkill(Skill):
    input_model = FileOperationInput
    output_model = FileOperationOutput

    def __init__(self, adapter: FileSystemAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.file_operation",
            display_name="File Operation",
            description="Copy, move, rename, recycle, or open an approved file.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.HIGH,
            supports_rollback=True,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        source = str(Path(args.source).resolve())
        if args.operation == "open":
            return (
                PermissionScope(
                    capability="filesystem.read",
                    targets=(source,),
                    reason="Open this exact approved file.",
                ),
            )
        targets = [source]
        if args.destination:
            targets.append(str(Path(args.destination).resolve()))
        return (
            PermissionScope(
                capability="filesystem.write",
                targets=tuple(targets),
                reason=f"{args.operation.title()} the exact displayed file target.",
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        operation = self.validate(call.arguments).operation
        return {
            "open": RiskLevel.MEDIUM,
            "copy": RiskLevel.MEDIUM,
            "move": RiskLevel.HIGH,
            "rename": RiskLevel.HIGH,
            "delete": RiskLevel.HIGH,
        }[operation]

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        destination = f" to {Path(args.destination).resolve()}" if args.destination else ""
        prefix = "Preview only: " if args.dry_run else ""
        return f"{prefix}{args.operation.title()} {Path(args.source).resolve()}{destination}"

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        source = Path(args.source).resolve()
        destination = Path(args.destination).resolve() if args.destination else None
        if not source.exists():
            raise FileNotFoundError(source)
        data = FileOperationOutput(
            operation=args.operation,
            source=str(source),
            destination=str(destination) if destination else None,
            dry_run=args.dry_run,
        ).model_dump()
        if args.dry_run:
            return SkillResult(
                status=ResultStatus.COMPLETED,
                summary=f"Previewed {args.operation}; no files were changed.",
                data=data,
            )
        rollback_token = None
        if args.operation == "copy":
            assert destination
            copied = await self.adapter.copy(source, destination)
            rollback_token = json.dumps(
                {"operation": "remove_copy", "path": str(copied)}
            )
        elif args.operation in {"move", "rename"}:
            assert destination
            await self.adapter.move(source, destination)
            rollback_token = json.dumps(
                {
                    "operation": "move_back",
                    "source": str(destination),
                    "destination": str(source),
                }
            )
        elif args.operation == "delete":
            await self.adapter.delete_to_recycle_bin(source)
        elif args.operation == "open":
            await self.adapter.open(source)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"{args.operation.title()} completed.",
            data=data,
            rollback_token=rollback_token,
        )

    async def rollback(self, token: str) -> SkillResult:
        payload = json.loads(token)
        if payload["operation"] == "remove_copy":
            path = Path(payload["path"])
            if path.exists():
                await self.adapter.delete_to_recycle_bin(path)
            summary = "Copied file moved to the Recycle Bin."
        elif payload["operation"] == "move_back":
            await self.adapter.move(
                Path(payload["source"]), Path(payload["destination"])
            )
            summary = "Moved file restored to its original path."
        else:
            raise ValueError("Unknown rollback token")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            data={"operation": "rollback"},
        )


class WindowInput(BaseModel):
    operation: Literal["focus", "minimize", "maximize", "close"]
    process_name: str = Field(pattern=r"^[^\\/:*?\"<>|]+\.exe$")


class WindowOutput(BaseModel):
    operation: str
    process_name: str


class ApplicationWindowSkill(Skill):
    input_model = WindowInput
    output_model = WindowOutput

    def __init__(self, adapter: ApplicationAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.application_window",
            display_name="Application Window",
            description="Focus, minimize, maximize, or close an approved application.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.HIGH,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return (
            PermissionScope(
                capability="process.window",
                targets=(args.process_name.casefold(),),
                reason=f"{args.operation.title()} this exact application.",
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.HIGH
            if self.validate(call.arguments).operation == "close"
            else RiskLevel.MEDIUM
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        return f"{args.operation.title()} application {args.process_name}"

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        operation = getattr(self.adapter, args.operation)
        changed = await operation(args.process_name)
        if not changed:
            raise RuntimeError(f"No visible window found for {args.process_name}")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"{args.operation.title()} completed for {args.process_name}.",
            data=WindowOutput(
                operation=args.operation, process_name=args.process_name
            ).model_dump(),
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
    operation: Literal["read", "set"]
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
        return (
            PermissionScope(
                capability=f"settings.{args.operation}",
                targets=(args.setting,),
                reason=f"{args.operation.title()} the approved Windows setting.",
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.MEDIUM
            if self.validate(call.arguments).operation == "set"
            else RiskLevel.LOW
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        return (
            f"Set Windows dark mode to {args.enabled}"
            if args.operation == "set"
            else "Read Windows dark mode"
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if args.operation == "set":
            await self.adapter.set_dark_mode(bool(args.enabled))
        enabled = await self.adapter.get_dark_mode()
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Windows dark mode is {'on' if enabled else 'off'}.",
            data=WindowsSettingOutput(
                setting=args.setting, enabled=enabled
            ).model_dump(),
        )
