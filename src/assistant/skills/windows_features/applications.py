from __future__ import annotations

import asyncio
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
from assistant.windows.interfaces import (
    ApplicationAdapter,
    ContextNavigationAdapter,
)
from assistant.windows.process_names import normalize_process_name


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
