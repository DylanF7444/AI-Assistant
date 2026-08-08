from __future__ import annotations

import ast
import asyncio
import math
import operator
import platform
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    RequestResultItem,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillManifest,
    SkillResult,
)
from assistant.policy.file_access import FileAccessPolicyStore
from assistant.skills.base import ApplicationRegistrationRequired, Skill
from assistant.windows.adapters import (
    ApplicationAdapter,
    ClipboardAdapter,
    FileSystemAdapter,
    MediaAdapter,
)
from assistant.windows.paths import resolve_user_path


class EmptyOutput(BaseModel):
    ok: bool = True


def manifest(
    *,
    skill_id: str,
    display_name: str,
    description: str,
    category: str | None = None,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    permissions: tuple[PermissionScope, ...] = (),
    risk: RiskLevel = RiskLevel.LOW,
    supports_rollback: bool = False,
    safe_to_retry: bool = False,
    background_mode: BackgroundMode = BackgroundMode.CAPABILITY_GATED,
) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        display_name=display_name,
        category=category or _category_for_skill(skill_id),
        description=description,
        input_schema=input_model.model_json_schema(),
        output_schema=output_model.model_json_schema(),
        permissions=permissions,
        risk=risk,
        entry_point=f"{__name__}:{display_name.replace(' ', '')}Skill",
        supports_rollback=supports_rollback,
        safe_to_retry=safe_to_retry,
        background_mode=background_mode,
    )


def _category_for_skill(skill_id: str) -> str:
    if skill_id in {
        "windows.file_search",
        "windows.file_operation",
        "documents.read",
        "documents.content_search",
        "vision.understand_image",
    }:
        return "Files & Folders"
    if skill_id.startswith("windows."):
        return "Windows"
    if skill_id.startswith(("web.", "cloud.")):
        return "Web & Cloud"
    if skill_id.startswith("integration."):
        return "Connections"
    if skill_id in {
        "core.timer",
        "core.datetime",
        "core.calculator",
        "core.chance",
        "core.choose",
        "core.tasks",
        "core.notes",
        "core.reminder",
        "core.unit_conversion",
        "core.schedule",
        "core.macro",
        "core.playbook",
        "core.visualize",
    }:
        return "Visualization" if skill_id == "core.visualize" else "Productivity"
    if skill_id.startswith("core."):
        return "Assistant & Security"
    return "General"


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
            "time": now.strftime("%-I:%M %p")
            if platform.system() != "Windows"
            else now.strftime("%I:%M %p").lstrip("0"),
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
        paths = await self.adapter.search(resolve_user_path(args.root), args.query, args.limit)
        values = [str(path) for path in paths]
        display = f"Found {len(values)} matching item{'s' if len(values) != 1 else ''}."
        if values:
            display += "\n" + "\n".join(f"{index}. {path}" for index, path in enumerate(values, 1))
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Found {len(values)} matching item{'s' if len(values) != 1 else ''}.",
            data=FileSearchOutput(paths=values).model_dump(),
            items=tuple(
                RequestResultItem(
                    item_id=f"file-{index}",
                    title=path,
                    value=path,
                    kind="file",
                    metadata={
                        "path": path,
                        "name": Path(path).name,
                        "extension": Path(path).suffix.casefold(),
                    },
                )
                for index, path in enumerate(values, 1)
            ),
            display_text=display,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return (
            PermissionScope(
                capability="filesystem.read",
                targets=(str(resolve_user_path(args.root)),),
                reason="Search filenames inside the selected folder.",
            ),
        )


class FileAccessPolicyInput(BaseModel):
    operation: Literal["add", "remove", "list"]
    list_name: Literal["whitelist", "blacklist"] | None = None
    access: Literal["read", "write", "delete", "write/delete"] | None = None
    path: str | None = None


class FileAccessPolicyOutput(BaseModel):
    read_whitelist: list[str]
    read_blacklist: list[str]
    write_whitelist: list[str]
    write_blacklist: list[str]


class FileAccessPolicySkill(Skill):
    input_model = FileAccessPolicyInput
    output_model = FileAccessPolicyOutput

    def __init__(self, store: FileAccessPolicyStore) -> None:
        self.store = store
        self.manifest = manifest(
            skill_id="core.file_access_policy",
            display_name="File Access Policy",
            description="List or change file read and write/delete allow and block lists.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="settings.get",
                    targets=("file_access_policy",),
                    reason="Read file access policy lists.",
                ),
                PermissionScope(
                    capability="settings.set",
                    targets=("file_access_policy",),
                    reason="Change file access policy lists.",
                ),
            ),
            risk=RiskLevel.HIGH,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        capability = "settings.get" if args.operation == "list" else "settings.set"
        reason = (
            "Read file access policy lists."
            if args.operation == "list"
            else "Change file access policy lists."
        )
        return (
            PermissionScope(
                capability=capability,
                targets=("file_access_policy",),
                reason=reason,
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.MEDIUM
            if self.validate(call.arguments).operation == "list"
            else RiskLevel.HIGH
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "list":
            return "Show file access policy lists"
        if not args.list_name or not args.access or not args.path:
            raise ValueError("add/remove requires list_name, access, and path")
        verb = "Add" if args.operation == "add" else "Remove"
        preposition = "to" if args.operation == "add" else "from"
        return (
            f"{verb} {resolve_user_path(args.path)} {preposition} the "
            f"{args.access} {args.list_name}"
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if args.operation == "list":
            policy = self.store.get()
            summary = "File access policy lists loaded."
        else:
            if not args.list_name or not args.access or not args.path:
                raise ValueError("add/remove requires list_name, access, and path")
            policy = self.store.update_entry(
                list_name=args.list_name,
                access=args.access,
                path=args.path,
                add=args.operation == "add",
            )
            summary = "File access policy updated."
        data = FileAccessPolicyOutput(**policy.to_mapping()).model_dump()
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            data=data,
            display_text=_format_file_policy(data),
        )


def _format_file_policy(data: dict[str, list[str]]) -> str:
    labels = {
        "read_whitelist": "Read whitelist",
        "read_blacklist": "Read blacklist",
        "write_whitelist": "Write/delete whitelist",
        "write_blacklist": "Write/delete blacklist",
    }
    lines = ["File access policy:"]
    for key, label in labels.items():
        values = data.get(key) or []
        lines.append(f"{label}:")
        lines.extend(f"- {value}" for value in values)
        if not values:
            lines.append("- None")
    return "\n".join(lines)


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


class RegisteredApplicationsInput(BaseModel):
    include_paths: bool = False


class RegisteredApplicationsOutput(BaseModel):
    applications: list[dict[str, str]]


class RegisteredApplicationsSkill(Skill):
    input_model = RegisteredApplicationsInput
    output_model = RegisteredApplicationsOutput

    def __init__(self, registrations: Callable[[], dict[str, str]]) -> None:
        self._registrations = registrations
        self.manifest = manifest(
            skill_id="windows.registered_applications",
            display_name="Registered Applications",
            description="List application paths registered in assistant settings for the launcher.",
            input_model=self.input_model,
            output_model=self.output_model,
            safe_to_retry=True,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        registrations = self._registrations()
        applications = _safe_distinct_application_registrations(
            [{"name": name, "path": path} for name, path in sorted(registrations.items())]
        )
        if not applications:
            return SkillResult(
                status=ResultStatus.COMPLETED,
                summary="No applications are registered yet.",
                display_text=(
                    "No applications are registered yet.\n\n"
                    "Open the Applications tab and choose Register Application "
                    "to add one."
                ),
                data=RegisteredApplicationsOutput(applications=[]).model_dump(),
            )
        display_limit = 20
        shown = applications[:display_limit]
        if args.include_paths:
            lines = [f"- {item['name']}: {item['path']}" for item in shown]
        else:
            lines = [f"- {item['name']}" for item in shown]
        if len(applications) > display_limit:
            lines.append(
                f"- …and {len(applications) - display_limit} more in the Applications tab."
            )
        count = len(applications)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=(f"{count} application{'s are' if count != 1 else ' is'} registered."),
            display_text="Registered applications:\n" + "\n".join(lines),
            data=RegisteredApplicationsOutput(applications=applications).model_dump(),
            sensitive=args.include_paths,
        )


def _safe_distinct_application_registrations(
    applications: list[dict[str, str]],
) -> list[dict[str, str]]:
    unsafe_name_parts = (
        "crashpad",
        "installer",
        "setup",
        "unins",
        "uninstall",
        "updater",
        "vcredist",
        "winsdksetup",
    )
    by_path: dict[str, dict[str, str]] = {}
    for item in applications:
        executable = Path(item["path"])
        folded_name = executable.name.casefold()
        if executable.suffix.casefold() != ".exe" or any(
            marker in folded_name for marker in unsafe_name_parts
        ):
            continue
        identity = str(executable).casefold()
        existing = by_path.get(identity)
        if existing is None or (len(item["name"]), item["name"].casefold()) < (
            len(existing["name"]),
            existing["name"].casefold(),
        ):
            by_path[identity] = item
    return sorted(by_path.values(), key=lambda item: item["name"].casefold())


class LauncherSkill(Skill):
    input_model = LaunchInput
    output_model = LaunchOutput
    PRIVATE_BROWSER_FLAGS = {
        "edge": "--inprivate",
        "microsoft edge": "--inprivate",
        "msedge": "--inprivate",
        "firefox": "-private-window",
        "mozilla firefox": "-private-window",
        "firefox developer edition": "-private-window",
        "firefox nightly": "-private-window",
        "librewolf": "-private-window",
        "waterfox": "-private-window",
        "opera": "--private",
        "opera browser": "--private",
        "opera gx": "--private",
    }

    def __init__(self, adapter: ApplicationAdapter, resolver=None) -> None:
        self.adapter = adapter
        self.resolver = resolver
        self.manifest = manifest(
            skill_id="windows.launcher",
            display_name="Launcher",
            description=(
                "Start or open an approved registered application, creating a new, "
                "fresh, or blank running instance and its initial window."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="process.launch",
                    reason="Start an application registered in assistant settings.",
                ),
            ),
            risk=RiskLevel.MEDIUM,
            background_mode=BackgroundMode.FOREGROUND_ONLY,
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
                    "Start this registered application path with the requested browser arguments."
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

    @classmethod
    def _private_browser_flag(cls, application: str, path: str) -> str:
        identity = cls._browser_identity(application, path)
        for browser, flag in cls.PRIVATE_BROWSER_FLAGS.items():
            if re.search(rf"\b{re.escape(browser)}\b", identity):
                return flag
        return "--incognito"

    @staticmethod
    def _browser_identity(application: str, path: str) -> str:
        identity = f"{application} {Path(path).stem}".casefold()
        return re.sub(r"[^a-z0-9]+", " ", identity)

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
                PermissionScope(capability="media.control", reason="Send a media control key."),
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
            background_mode=BackgroundMode.FOREGROUND_ONLY,
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
