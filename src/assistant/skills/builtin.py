from __future__ import annotations

import ast
import asyncio
import math
import operator
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic import field_validator

from assistant.contracts import (
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillManifest,
    SkillResult,
)
from assistant.skills.base import Skill
from assistant.skills.base import ApplicationRegistrationRequired
from assistant.windows.adapters import (
    ApplicationAdapter,
    ClipboardAdapter,
    FileSystemAdapter,
    MediaAdapter,
)


class EmptyOutput(BaseModel):
    ok: bool = True


def manifest(
    *,
    skill_id: str,
    display_name: str,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    permissions: tuple[PermissionScope, ...] = (),
    risk: RiskLevel = RiskLevel.LOW,
    supports_rollback: bool = False,
    safe_to_retry: bool = False,
) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        display_name=display_name,
        description=description,
        input_schema=input_model.model_json_schema(),
        output_schema=output_model.model_json_schema(),
        permissions=permissions,
        risk=risk,
        entry_point=f"{__name__}:{display_name.replace(' ', '')}Skill",
        supports_rollback=supports_rollback,
        safe_to_retry=safe_to_retry,
    )


class TimerInput(BaseModel):
    seconds: float = Field(gt=0, le=86400)
    label: str = "Timer"


class TimerOutput(BaseModel):
    elapsed_seconds: float
    label: str


class TimerSkill(Skill):
    input_model = TimerInput
    output_model = TimerOutput
    manifest = manifest(
        skill_id="core.timer",
        display_name="Timer",
        description="Run a local cancellable timer.",
        input_model=input_model,
        output_model=output_model,
    )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        started = time.monotonic()
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=args.seconds)
            return SkillResult(status=ResultStatus.CANCELLED, summary=f"{args.label} cancelled.")
        except TimeoutError:
            elapsed = time.monotonic() - started
            return SkillResult(
                status=ResultStatus.COMPLETED,
                summary=f"{args.label} finished.",
                data=TimerOutput(elapsed_seconds=elapsed, label=args.label).model_dump(),
            )


class DateTimeInput(BaseModel):
    query: Literal["date", "time", "both"] = "both"


class DateTimeOutput(BaseModel):
    iso_datetime: str


class DateTimeSkill(Skill):
    input_model = DateTimeInput
    output_model = DateTimeOutput
    manifest = manifest(
        skill_id="core.datetime",
        display_name="Date Time",
        description="Return the local date and time.",
        input_model=input_model,
        output_model=output_model,
        safe_to_retry=True,
    )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        now = datetime.now().astimezone()
        text = {
            "date": now.strftime("%A, %B %d, %Y"),
            "time": now.strftime("%-I:%M %p") if platform.system() != "Windows" else now.strftime("%I:%M %p").lstrip("0"),
            "both": now.strftime("%A, %B %d, %Y at %I:%M %p").replace(" at 0", " at "),
        }[args.query]
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=text,
            data=DateTimeOutput(iso_datetime=now.isoformat()).model_dump(),
        )


class CalculatorInput(BaseModel):
    expression: str = Field(min_length=1, max_length=500)


class CalculatorOutput(BaseModel):
    value: float


_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("Exponent is too large")
        return float(_OPS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return float(_OPS[type(node.op)](_safe_eval(node.operand)))
    raise ValueError("Expression contains unsupported operations")


class CalculatorSkill(Skill):
    input_model = CalculatorInput
    output_model = CalculatorOutput
    manifest = manifest(
        skill_id="core.calculator",
        display_name="Calculator",
        description="Safely evaluate arithmetic without executing code.",
        input_model=input_model,
        output_model=output_model,
        safe_to_retry=True,
    )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        value = _safe_eval(ast.parse(args.expression, mode="eval"))
        if not math.isfinite(value):
            raise ValueError("Result is not finite")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"The result is {value:g}.",
            data=CalculatorOutput(value=value).model_dump(),
        )


class FileSearchInput(BaseModel):
    root: str
    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=100)


class FileSearchOutput(BaseModel):
    paths: list[str]


class FileSearchSkill(Skill):
    input_model = FileSearchInput
    output_model = FileSearchOutput

    def __init__(self, adapter: FileSystemAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.file_search",
            display_name="File Search",
            description="Search filenames within an approved folder.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="filesystem.read",
                    targets=(),
                    reason="Search filenames inside the selected folder.",
                ),
            ),
            safe_to_retry=True,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        paths = await self.adapter.search(Path(args.root), args.query, args.limit)
        values = [str(path) for path in paths]
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Found {len(values)} matching item{'s' if len(values) != 1 else ''}.",
            data=FileSearchOutput(paths=values).model_dump(),
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return (
            PermissionScope(
                capability="filesystem.read",
                targets=(str(Path(args.root).resolve()),),
                reason="Search filenames inside the selected folder.",
            ),
        )


class LaunchInput(BaseModel):
    application: str
    registered_path: str | None = None
    url: str | None = Field(default=None, max_length=2000)
    fallback_url: str | None = Field(default=None, max_length=2000)
    private: bool = False

    @field_validator("url", "fallback_url")
    @classmethod
    def validate_url(cls, value):
        if value is None:
            return value
        if not isinstance(value, str) or not value.startswith("https://"):
            raise ValueError("launcher URL must be public HTTPS")
        return value


class LaunchOutput(BaseModel):
    application: str
    private: bool = False


class LauncherSkill(Skill):
    input_model = LaunchInput
    output_model = LaunchOutput

    def __init__(self, adapter: ApplicationAdapter, resolver=None) -> None:
        self.adapter = adapter
        self.resolver = resolver
        self.manifest = manifest(
            skill_id="windows.launcher",
            display_name="Launcher",
            description="Launch an approved registered application path.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="process.launch",
                    reason="Start an application registered in assistant settings.",
                ),
            ),
            risk=RiskLevel.MEDIUM,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        path = self._resolved_path(args)
        if path:
            launch_args = self._launch_arguments(args, path)
            if launch_args:
                await self.adapter.launch_with_args(path, launch_args)
            else:
                await self.adapter.launch(path)
            destination = f" to {args.url}" if args.url else ""
        else:
            if not args.fallback_url:
                raise ApplicationRegistrationRequired(args.application)
            await self.adapter.open_url(args.fallback_url or "")
            destination = f" to {args.fallback_url}"
        mode = " in a private window" if args.private else ""
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Opened {args.application}{destination}{mode}.",
            data=LaunchOutput(
                application=args.application,
                private=args.private,
            ).model_dump(),
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        path = self._resolved_path(args)
        if path is None:
            if args.fallback_url:
                return (
                    PermissionScope(
                        capability="network",
                        targets=(args.fallback_url,),
                        reason="Open this public HTTPS website in the default browser.",
                    ),
                )
            raise ApplicationRegistrationRequired(args.application)
        return (
            PermissionScope(
                capability="process.launch",
                targets=(str(Path(path).resolve()),),
                reason=(
                    "Start this registered application path with the requested "
                    "browser arguments."
                    if args.url or args.private
                    else "Start this registered application path."
                ),
            ),
        )

    @classmethod
    def _launch_arguments(cls, args: LaunchInput, path: str) -> tuple[str, ...]:
        launch_args: list[str] = []
        if args.private:
            launch_args.append(cls._private_browser_flag(args.application, path))
        if args.url:
            launch_args.append(args.url)
        return tuple(launch_args)

    @staticmethod
    def _private_browser_flag(application: str, path: str) -> str:
        identity = f"{application} {Path(path).stem}".casefold()
        if "firefox" in identity:
            return "-private-window"
        if "edge" in identity or "msedge" in identity:
            return "--inprivate"
        return "--incognito"

    def _registered_path(self, args: LaunchInput) -> str:
        registered = self._resolved_path(args)
        if registered is None:
            raise ApplicationRegistrationRequired(args.application)
        return registered

    def _resolved_path(self, args: LaunchInput) -> str | None:
        registered = self.resolver(args.application) if self.resolver else None
        if registered is None:
            return None
        if args.registered_path and (
            Path(args.registered_path).resolve() != Path(registered).resolve()
        ):
            raise ValueError("Requested path does not match the registered application")
        return registered


class MediaInput(BaseModel):
    action: Literal[
        "play_pause",
        "next",
        "previous",
        "stop",
        "volume_mute",
        "volume_down",
        "volume_up",
    ]


class MediaOutput(BaseModel):
    action: str


class MediaSkill(Skill):
    input_model = MediaInput
    output_model = MediaOutput

    def __init__(self, adapter: MediaAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.media",
            display_name="Media",
            description="Control local media playback.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="media.control", reason="Send a media control key."
                ),
            ),
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        await self.adapter.command(args.action)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Media command {args.action.replace('_', ' ')} sent.",
            data=MediaOutput(action=args.action).model_dump(),
        )


class ClipboardInput(BaseModel):
    operation: Literal["read", "write"]
    text: str | None = None


class ClipboardOutput(BaseModel):
    text: str | None = None


class ClipboardSkill(Skill):
    input_model = ClipboardInput
    output_model = ClipboardOutput

    def __init__(self, adapter: ClipboardAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.clipboard",
            display_name="Clipboard",
            description="Read or write the local clipboard.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.MEDIUM,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return (
            PermissionScope(
                capability=f"clipboard.{args.operation}",
                reason=f"{args.operation.title()} local clipboard content.",
            ),
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if args.operation == "read":
            text = await self.adapter.read()
            return SkillResult(
                status=ResultStatus.COMPLETED,
                summary="Clipboard content is available on screen.",
                display_text=text,
                data=ClipboardOutput(text=text).model_dump(),
                sensitive=True,
            )
        if args.text is None:
            raise ValueError("Clipboard write requires text")
        await self.adapter.write(args.text)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Copied text to the clipboard.",
            data=ClipboardOutput().model_dump(),
            sensitive=True,
        )


class HelpInput(BaseModel):
    topic: str | None = None


class HelpOutput(BaseModel):
    skills: list[dict[str, Any]]


class HelpSkill(Skill):
    input_model = HelpInput
    output_model = HelpOutput

    def __init__(self, manifests: callable) -> None:
        self._manifests = manifests
        self.manifest = manifest(
            skill_id="core.help",
            display_name="Help",
            description="Explain available commands and permissions.",
            input_model=self.input_model,
            output_model=self.output_model,
            safe_to_retry=True,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        entries = [
            {
                "id": item.skill_id,
                "name": item.display_name,
                "description": item.description,
                "permissions": [scope.capability for scope in item.permissions],
            }
            for item in self._manifests()
            if not args.topic
            or args.topic.casefold() in item.display_name.casefold()
            or args.topic.casefold() in item.description.casefold()
        ]
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"{len(entries)} skills are available.",
            data=HelpOutput(skills=entries).model_dump(),
        )
