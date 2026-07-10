from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from assistant.contracts import (
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillError,
    SkillResult,
)
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.windows.adapters import (
    ApplicationAdapter,
    ContextNavigationAdapter,
    FileSystemAdapter,
    InputAutomationAdapter,
    NotificationAdapter,
    ProcessAdapter,
    WindowsDeviceAdapter,
    WindowsSettingsAdapter,
)
from assistant.windows.paths import resolve_user_path
from assistant.windows.process_names import normalize_process_name


class FileOperationInput(BaseModel):
    operation: Literal["copy", "move", "rename", "delete", "open", "create_text"]
    source: str
    destination: str | None = None
    content: str = ""
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
            description=(
                "Create text files, copy, move, rename, recycle, or open an approved file."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.HIGH,
            supports_rollback=True,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        source = str(resolve_user_path(args.source))
        if args.operation == "create_text":
            return (
                PermissionScope(
                    capability="filesystem.write",
                    targets=(source,),
                    reason="Create this exact approved text file.",
                ),
            )
        if args.operation == "open":
            return (
                PermissionScope(
                    capability="filesystem.read",
                    targets=(source,),
                    reason="Open this exact approved file.",
                ),
            )
        if args.operation == "copy":
            assert args.destination
            return (
                PermissionScope(
                    capability="filesystem.read",
                    targets=(source,),
                    reason="Copy from this exact displayed source file.",
                ),
                PermissionScope(
                    capability="filesystem.write",
                    targets=(str(resolve_user_path(args.destination)),),
                    reason="Copy to this exact displayed destination.",
                ),
            )
        targets = [source]
        if args.destination:
            targets.append(str(resolve_user_path(args.destination)))
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
            "create_text": RiskLevel.MEDIUM,
            "move": RiskLevel.HIGH,
            "rename": RiskLevel.HIGH,
            "delete": RiskLevel.HIGH,
        }[operation]

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        destination = f" to {resolve_user_path(args.destination)}" if args.destination else ""
        prefix = "Preview only: " if args.dry_run else ""
        return (
            f"{prefix}{_file_operation_label(args.operation)} "
            f"{resolve_user_path(args.source)}{destination}"
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        source = resolve_user_path(args.source)
        destination = resolve_user_path(args.destination) if args.destination else None
        if args.operation == "create_text":
            if source.exists():
                raise FileExistsError(source)
        elif not source.exists():
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
        if args.operation == "create_text":
            created = await self.adapter.create_text_file(source, args.content)
            rollback_token = json.dumps({"operation": "remove_created_file", "path": str(created)})
        elif args.operation == "copy":
            assert destination
            copied = await self.adapter.copy(source, destination)
            rollback_token = json.dumps({"operation": "remove_copy", "path": str(copied)})
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
            summary=f"{_file_operation_label(args.operation)} completed.",
            data=data,
            rollback_token=rollback_token,
        )

    async def rollback(self, token: str) -> SkillResult:
        payload = json.loads(token)
        if payload["operation"] in {"remove_copy", "remove_created_file"}:
            path = Path(payload["path"])
            if path.exists():
                await self.adapter.delete_to_recycle_bin(path)
            summary = (
                "Created file moved to the Recycle Bin."
                if payload["operation"] == "remove_created_file"
                else "Copied file moved to the Recycle Bin."
            )
        elif payload["operation"] == "move_back":
            await self.adapter.move(Path(payload["source"]), Path(payload["destination"]))
            summary = "Moved file restored to its original path."
        else:
            raise ValueError("Unknown rollback token")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            data={"operation": "rollback"},
        )


def _file_operation_label(operation: str) -> str:
    return "Create text file" if operation == "create_text" else operation.title()


class WindowInput(BaseModel):
    operation: Literal[
        "focus",
        "minimize",
        "maximize",
        "minimize_focused",
        "maximize_focused",
        "close",
        "move_to_monitor",
        "move",
        "resize",
    ]
    process_name: str | None = Field(default=None, pattern=r"^[^\\/:*?\"<>|]+\.exe$")
    monitor: int | None = Field(default=None, ge=1)
    x: int | None = None
    y: int | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)

    @field_validator("process_name", mode="before")
    @classmethod
    def validate_process_name(cls, value):
        if not isinstance(value, str):
            return value
        return normalize_process_name(value)

    @model_validator(mode="after")
    def operation_arguments(self):
        if (
            self.operation
            in {
                "focus",
                "minimize",
                "maximize",
                "close",
                "move_to_monitor",
                "move",
                "resize",
            }
            and not self.process_name
        ):
            raise ValueError(f"{self.operation} requires process_name")
        if self.operation == "move_to_monitor" and self.monitor is None:
            raise ValueError("move_to_monitor requires monitor")
        if self.operation == "move" and (self.x is None or self.y is None):
            raise ValueError("move requires x and y")
        if self.operation == "resize" and (self.width is None or self.height is None):
            raise ValueError("resize requires width and height")
        return self


class WindowOutput(BaseModel):
    operation: str
    process_name: str | None = None
    monitor: int | None = None
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None


class ApplicationWindowSkill(Skill):
    input_model = WindowInput
    output_model = WindowOutput

    def __init__(self, adapter: ApplicationAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.application_window",
            display_name="Application Window",
            description="Focus, minimize, maximize, close, move, or resize an approved application window.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.HIGH,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        target = (
            "focused-window"
            if args.operation in {"minimize_focused", "maximize_focused"}
            else (args.process_name or "").casefold()
        )
        return (
            PermissionScope(
                capability="process.window",
                targets=(target,),
                reason=f"{args.operation.replace('_', ' ').title()} this exact application window.",
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
        if args.operation in {"minimize_focused", "maximize_focused"}:
            return f"{args.operation.removesuffix('_focused').title()} focused window"
        if args.operation == "move_to_monitor":
            return f"Move application {args.process_name} to monitor {args.monitor}"
        if args.operation == "move":
            return f"Move application {args.process_name} to ({args.x}, {args.y})"
        if args.operation == "resize":
            return f"Resize application {args.process_name} to {args.width} by {args.height}"
        return f"{args.operation.title()} application {args.process_name}"

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if args.operation in {"minimize_focused", "maximize_focused"}:
            operation = getattr(self.adapter, args.operation)
            changed = await operation()
        elif args.operation == "move_to_monitor":
            changed = await self.adapter.move_to_monitor(args.process_name, args.monitor or 1)
        elif args.operation == "move":
            changed = await self.adapter.move(args.process_name, args.x or 0, args.y or 0)
        elif args.operation == "resize":
            changed = await self.adapter.resize(
                args.process_name, args.width or 1, args.height or 1
            )
        else:
            operation = getattr(self.adapter, args.operation)
            changed = await operation(args.process_name)
        if not changed:
            if args.operation in {"minimize_focused", "maximize_focused"}:
                raise RuntimeError("No focused window found")
            raise RuntimeError(f"No visible window found for {args.process_name}")
        target = args.process_name or "focused window"
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=(f"{args.operation.replace('_', ' ').title()} completed for {target}."),
            data=WindowOutput(
                operation=args.operation,
                process_name=args.process_name,
                monitor=args.monitor,
                x=args.x,
                y=args.y,
                width=args.width,
                height=args.height,
            ).model_dump(),
        )


class ContextNavigationInput(BaseModel):
    target: Literal["browser"] = "browser"
    operation: Literal[
        "new_tab",
        "new_window",
        "close_tab",
        "reopen_closed_tab",
        "next_tab",
        "previous_tab",
        "focus_address_bar",
        "back",
        "forward",
        "reload",
    ]


class ContextNavigationOutput(BaseModel):
    target: str
    operation: str
    process_name: str


class ContextNavigationSkill(Skill):
    input_model = ContextNavigationInput
    output_model = ContextNavigationOutput
    BROWSER_PROCESSES = (
        "brave.exe",
        "chrome.exe",
        "chromium.exe",
        "msedge.exe",
        "firefox.exe",
        "opera.exe",
        "opera_gx.exe",
        "vivaldi.exe",
        "librewolf.exe",
        "waterfox.exe",
    )
    HOTKEYS = {
        "new_tab": ("ctrl", "t"),
        "new_window": ("ctrl", "n"),
        "close_tab": ("ctrl", "w"),
        "reopen_closed_tab": ("ctrl", "shift", "t"),
        "next_tab": ("ctrl", "tab"),
        "previous_tab": ("ctrl", "shift", "tab"),
        "focus_address_bar": ("ctrl", "l"),
        "back": ("alt", "left"),
        "forward": ("alt", "right"),
        "reload": ("ctrl", "r"),
    }
    LABELS = {
        "new_tab": "Open a new browser tab",
        "new_window": "Open a new browser window",
        "close_tab": "Close the current browser tab",
        "reopen_closed_tab": "Reopen the last closed browser tab",
        "next_tab": "Switch to the next browser tab",
        "previous_tab": "Switch to the previous browser tab",
        "focus_address_bar": "Focus the browser address bar",
        "back": "Go back in the browser",
        "forward": "Go forward in the browser",
        "reload": "Reload the current browser page",
    }

    def __init__(self, adapter: ContextNavigationAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.context_navigation",
            display_name="Context Navigation",
            description=(
                "Use the focused or visible browser window for tab, page, and "
                "address-bar navigation shortcuts."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.MEDIUM,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return (
            PermissionScope(
                capability="process.window",
                targets=(args.target,),
                reason="Focus the focused or visible browser window.",
            ),
            PermissionScope(
                capability="input.control",
                targets=(args.target,),
                reason="Send the requested browser navigation shortcut.",
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        operation = self.validate(call.arguments).operation
        return (
            RiskLevel.MEDIUM if operation in {"close_tab", "reopen_closed_tab"} else RiskLevel.LOW
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        return self.LABELS[args.operation]

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        target_process = await self._target_process(args.target)
        if target_process is None:
            raise RuntimeError("No focused or visible browser window was found")
        process_name, needs_focus = target_process
        if needs_focus:
            if not await self.adapter.focus(process_name):
                raise RuntimeError(f"No visible browser window found for {process_name}")
            await asyncio.sleep(0.05)
        await self.adapter.hotkey(self.HOTKEYS[args.operation])
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"{self.LABELS[args.operation]} completed.",
            data=ContextNavigationOutput(
                target=args.target,
                operation=args.operation,
                process_name=process_name,
            ).model_dump(),
        )

    async def _target_process(self, target: str) -> tuple[str, bool] | None:
        if target != "browser":
            return None
        browser_names = {
            normalize_process_name(process).casefold() for process in self.BROWSER_PROCESSES
        }
        active = await self.adapter.active_process_name()
        if active and normalize_process_name(active).casefold() in browser_names:
            return normalize_process_name(active), False
        visible = await self.adapter.first_visible_process(self.BROWSER_PROCESSES)
        return (visible, True) if visible else None


class BrowserNavigateInput(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    target: Literal["browser"] = "browser"

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        if not isinstance(value, str) or not value.startswith("https://"):
            raise ValueError("browser URL must be public HTTPS")
        return value


class BrowserNavigateOutput(BaseModel):
    url: str
    process_name: str | None = None


class BrowserNavigateSkill(Skill):
    input_model = BrowserNavigateInput
    output_model = BrowserNavigateOutput
    BROWSER_PROCESSES = ContextNavigationSkill.BROWSER_PROCESSES

    def __init__(self, adapter: ContextNavigationAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.browser_navigate",
            display_name="Browser Navigate",
            description="Open a public HTTPS website in an existing browser window.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.LOW,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return (
            PermissionScope(
                capability="process.window",
                targets=(args.target,),
                reason="Focus an existing browser window.",
            ),
            PermissionScope(
                capability="input.control",
                targets=(args.target,),
                reason="Type the website into the browser address bar.",
            ),
            PermissionScope(
                capability="network",
                targets=(args.url,),
                reason="Open this public HTTPS website in the browser.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        return f"Open {args.url} in an existing browser window"

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        target = await self._target_process(args.target)
        if target == "ambiguous":
            return SkillResult(
                status=ResultStatus.DENIED,
                summary=(
                    "Which browser window should I use? Focus the private or "
                    "regular browser window you want, then ask again."
                ),
                data=BrowserNavigateOutput(url=args.url).model_dump(),
                error=SkillError(
                    code="clarification_required",
                    message="Both private and regular browser windows are open.",
                    recoverable=True,
                ),
            )
        if target is None:
            raise RuntimeError("No visible browser window was found")
        process_name, needs_focus = target
        if needs_focus:
            if not await self.adapter.focus(process_name):
                raise RuntimeError(f"No visible browser window found for {process_name}")
            await asyncio.sleep(0.05)
        await self.adapter.hotkey(("ctrl", "l"))
        await asyncio.sleep(0.05)
        await self.adapter.type_text(args.url)
        await self.adapter.hotkey(("enter",))
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Opened {args.url} in the current browser window.",
            data=BrowserNavigateOutput(
                url=args.url,
                process_name=process_name,
            ).model_dump(),
        )

    async def _target_process(self, target: str) -> tuple[str, bool] | Literal["ambiguous"] | None:
        if target != "browser":
            return None
        browser_names = {
            normalize_process_name(process).casefold() for process in self.BROWSER_PROCESSES
        }
        active = await self.adapter.active_process_name()
        if active and normalize_process_name(active).casefold() in browser_names:
            return normalize_process_name(active), False
        windows = await self.adapter.visible_browser_windows(self.BROWSER_PROCESSES)
        if {window.private for window in windows} == {False, True}:
            return "ambiguous"
        if windows:
            return windows[0].process_name, True
        visible = await self.adapter.first_visible_process(self.BROWSER_PROCESSES)
        return (visible, True) if visible else None


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


class DeviceControlInput(BaseModel):
    operation: Literal[
        "search_installed_apps",
        "set_volume",
        "change_volume",
        "mute_audio",
        "unmute_audio",
        "set_brightness",
        "change_brightness",
        "toggle_wifi",
        "connect_wifi_network",
        "disconnect_wifi_network",
        "toggle_bluetooth",
        "connect_bluetooth_device",
        "disconnect_bluetooth_device",
        "lock_device",
        "restart_device",
        "shutdown_device",
        "battery_status",
        "storage_status",
        "performance_status",
        "connected_devices",
        "enable_connected_device",
        "disable_connected_device",
    ]
    query: str | None = Field(default=None, max_length=200)
    level: int | None = Field(default=None, ge=0, le=100)
    delta: int | None = Field(default=None, ge=-100, le=100)
    enabled: bool | None = None
    network_name: str | None = Field(default=None, min_length=1, max_length=100)
    device_name: str | None = Field(default=None, min_length=1, max_length=200)
    drive: str | None = Field(default=None, pattern=r"^[A-Za-z]:?\\?$")
    instance_id: str | None = Field(default=None, min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=200)

    @model_validator(mode="after")
    def operation_arguments(self):
        if self.operation in {"set_volume", "set_brightness"} and self.level is None:
            raise ValueError(f"{self.operation} requires level")
        if self.operation in {"change_volume", "change_brightness"} and not self.delta:
            raise ValueError(f"{self.operation} requires a non-zero delta")
        if (
            self.operation in {"enable_connected_device", "disable_connected_device"}
            and not self.instance_id
        ):
            raise ValueError(f"{self.operation} requires instance_id")
        if self.operation == "connect_wifi_network" and not self.network_name:
            raise ValueError("connect_wifi_network requires network_name")
        if (
            self.operation in {"connect_bluetooth_device", "disconnect_bluetooth_device"}
            and not self.device_name
        ):
            raise ValueError(f"{self.operation} requires device_name")
        return self


class DeviceControlOutput(BaseModel):
    operation: str
    result: object


class WindowsDeviceControlSkill(Skill):
    input_model = DeviceControlInput
    output_model = DeviceControlOutput

    READ_OPERATIONS = {
        "battery_status",
        "storage_status",
        "performance_status",
        "connected_devices",
    }
    AUDIO_OPERATIONS = {"set_volume", "change_volume", "mute_audio", "unmute_audio"}
    DISPLAY_OPERATIONS = {"set_brightness", "change_brightness"}
    NETWORK_OPERATIONS = {
        "toggle_wifi",
        "connect_wifi_network",
        "disconnect_wifi_network",
        "toggle_bluetooth",
        "connect_bluetooth_device",
        "disconnect_bluetooth_device",
    }
    POWER_OPERATIONS = {"lock_device", "restart_device", "shutdown_device"}

    def __init__(self, adapter: WindowsDeviceAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.device_control",
            display_name="Windows Device Control",
            description="Control local Windows device settings and read device status.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.CRITICAL,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        capability = self._capability(args)
        target = self._target(args)
        return (
            PermissionScope(
                capability=capability,
                targets=(target,),
                reason=f"{args.operation.replace('_', ' ').title()} on this Windows device.",
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        operation = self.validate(call.arguments).operation
        if operation in {"restart_device", "shutdown_device"}:
            return RiskLevel.CRITICAL
        if operation == "lock_device":
            return RiskLevel.HIGH
        if operation in {"enable_connected_device", "disable_connected_device"}:
            return RiskLevel.HIGH
        if operation in self.READ_OPERATIONS or operation == "search_installed_apps":
            return RiskLevel.LOW
        return RiskLevel.MEDIUM

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        name = args.operation.replace("_", " ")
        if args.operation in {"set_volume", "set_brightness"}:
            return f"{name.title()} to {args.level}%"
        if args.operation in {"change_volume", "change_brightness"}:
            return f"{name.title()} by {args.delta}%"
        if args.operation in {"toggle_wifi", "toggle_bluetooth"}:
            state = "toggle" if args.enabled is None else ("on" if args.enabled else "off")
            return f"{name.title()} {state}"
        if args.operation in {"connect_wifi_network", "disconnect_wifi_network"}:
            target = args.network_name or "current network"
            return f"{name.title()} {target}"
        if args.operation in {"connect_bluetooth_device", "disconnect_bluetooth_device"}:
            return f"{name.title()} {args.device_name}"
        if args.operation in {"enable_connected_device", "disable_connected_device"}:
            return f"{name.title()} {args.instance_id}"
        return name.title()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        result: object
        if args.operation == "search_installed_apps":
            result = await self.adapter.search_installed_apps(args.query, args.limit)
            summary = f"Found {len(result)} installed application{'s' if len(result) != 1 else ''}."
        elif args.operation == "set_volume":
            await self.adapter.set_volume(args.level or 0)
            result = {"level": args.level}
            summary = f"Volume set to {args.level}%."
        elif args.operation == "change_volume":
            await self.adapter.change_volume(args.delta or 0)
            result = {"delta": args.delta}
            summary = "Volume adjusted."
        elif args.operation == "mute_audio":
            await self.adapter.mute_audio(True)
            result = {"muted": True}
            summary = "Audio muted."
        elif args.operation == "unmute_audio":
            await self.adapter.mute_audio(False)
            result = {"muted": False}
            summary = "Audio unmuted."
        elif args.operation == "set_brightness":
            await self.adapter.set_brightness(args.level or 0)
            result = {"level": args.level}
            summary = f"Brightness set to {args.level}%."
        elif args.operation == "change_brightness":
            level = await self.adapter.change_brightness(args.delta or 0)
            result = {"level": level, "delta": args.delta}
            summary = f"Brightness adjusted to {level}%."
        elif args.operation == "toggle_wifi":
            enabled = await self.adapter.set_wifi_enabled(args.enabled)
            result = {"enabled": enabled}
            summary = f"Wi-Fi turned {'on' if enabled else 'off'}."
        elif args.operation == "connect_wifi_network":
            await self.adapter.connect_wifi_network(args.network_name or "")
            result = {"network_name": args.network_name, "connected": True}
            summary = f"Connected to Wi-Fi network {args.network_name}."
        elif args.operation == "disconnect_wifi_network":
            await self.adapter.disconnect_wifi_network(args.network_name)
            result = {"network_name": args.network_name, "connected": False}
            summary = (
                f"Disconnected from Wi-Fi network {args.network_name}."
                if args.network_name
                else "Disconnected from Wi-Fi."
            )
        elif args.operation == "toggle_bluetooth":
            enabled = await self.adapter.set_bluetooth_enabled(args.enabled)
            result = {"enabled": enabled}
            summary = f"Bluetooth turned {'on' if enabled else 'off'}."
        elif args.operation == "connect_bluetooth_device":
            await self.adapter.connect_bluetooth_device(args.device_name or "")
            result = {"device_name": args.device_name, "connected": True}
            summary = f"Connected Bluetooth device {args.device_name}."
        elif args.operation == "disconnect_bluetooth_device":
            await self.adapter.disconnect_bluetooth_device(args.device_name or "")
            result = {"device_name": args.device_name, "connected": False}
            summary = f"Disconnected Bluetooth device {args.device_name}."
        elif args.operation == "lock_device":
            await self.adapter.lock_device()
            result = {"locked": True}
            summary = "Device locked."
        elif args.operation == "restart_device":
            await self.adapter.restart_device()
            result = {"restart_requested": True}
            summary = "Restart requested."
        elif args.operation == "shutdown_device":
            await self.adapter.shutdown_device()
            result = {"shutdown_requested": True}
            summary = "Shutdown requested."
        elif args.operation == "battery_status":
            result = await self.adapter.battery_status()
            summary = "Battery status retrieved."
        elif args.operation == "storage_status":
            result = await self.adapter.storage_status(args.drive)
            summary = "Storage status retrieved."
        elif args.operation == "performance_status":
            result = await self.adapter.performance_status()
            summary = "CPU and memory usage retrieved."
        elif args.operation == "connected_devices":
            result = await self.adapter.connected_devices(args.limit)
            summary = f"Found {len(result)} connected device{'s' if len(result) != 1 else ''}."
        elif args.operation == "enable_connected_device":
            await self.adapter.set_connected_device_enabled(args.instance_id or "", True)
            result = {"instance_id": args.instance_id, "enabled": True}
            summary = "Connected device enabled."
        elif args.operation == "disable_connected_device":
            await self.adapter.set_connected_device_enabled(args.instance_id or "", False)
            result = {"instance_id": args.instance_id, "enabled": False}
            summary = "Connected device disabled."
        else:
            raise ValueError(f"Unsupported device operation: {args.operation}")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            data=DeviceControlOutput(operation=args.operation, result=result).model_dump(),
            sensitive=args.operation in self.READ_OPERATIONS
            or args.operation == "search_installed_apps",
        )

    @classmethod
    def _capability(cls, args: DeviceControlInput) -> str:
        if args.operation == "search_installed_apps":
            return "system.apps"
        if args.operation in cls.AUDIO_OPERATIONS:
            return "media.control"
        if args.operation in cls.DISPLAY_OPERATIONS:
            return "system.display"
        if args.operation in cls.NETWORK_OPERATIONS:
            return "system.network"
        if args.operation in cls.POWER_OPERATIONS:
            return "system.power"
        if args.operation in {"enable_connected_device", "disable_connected_device"}:
            return "device.control"
        return "device.read"

    @staticmethod
    def _target(args: DeviceControlInput) -> str:
        if args.operation == "storage_status" and args.drive:
            return args.drive.upper().rstrip("\\")
        if args.operation == "search_installed_apps":
            return args.query or "*"
        if args.operation == "toggle_wifi":
            return "wi-fi"
        if args.operation in {"connect_wifi_network", "disconnect_wifi_network"}:
            return args.network_name or "wi-fi"
        if args.operation == "toggle_bluetooth":
            return "bluetooth"
        if args.operation in {"connect_bluetooth_device", "disconnect_bluetooth_device"}:
            return args.device_name or "bluetooth"
        if args.operation in {"set_volume", "change_volume", "mute_audio", "unmute_audio"}:
            return "system-audio"
        if args.operation in {"set_brightness", "change_brightness"}:
            return "screen-brightness"
        if args.operation in {"enable_connected_device", "disable_connected_device"}:
            return args.instance_id or "*"
        return args.operation
