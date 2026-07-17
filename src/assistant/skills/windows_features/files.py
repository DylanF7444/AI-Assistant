from __future__ import annotations

import asyncio
import json
import re
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
from assistant.windows.interfaces import (
    FileSystemAdapter,
)
from assistant.windows.paths import resolve_user_path

_ALLOWED_PATH_PLACEHOLDERS = {
    "${assistant_folder}",
    "${user_desktop}",
    "${user_documents}",
    "${user_downloads}",
    "${user_pictures}",
    "${user_music}",
    "${user_videos}",
}

_ORGANIZATION_CATEGORY_EXTENSIONS = {
    "images": [
        ".bmp",
        ".gif",
        ".heic",
        ".jpeg",
        ".jpg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    ],
    "text_files": [
        ".csv",
        ".log",
        ".md",
        ".rtf",
        ".text",
        ".tsv",
        ".txt",
    ],
}


class FileOperationInput(BaseModel):
    operation: Literal[
        "copy",
        "move",
        "rename",
        "delete",
        "open",
        "create_text",
        "organize",
    ]
    source: str
    destination: str | None = None
    content: str = ""
    dry_run: bool = False
    rules: dict[str, list[str]] = Field(default_factory=dict)
    categories: dict[str, Literal["images", "text_files"]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def destination_required(self):
        if self.operation in {"copy", "move", "rename"} and not self.destination:
            raise ValueError(f"{self.operation} requires a destination")
        if self.operation == "organize":
            if self.destination:
                raise ValueError("organize uses rule folder names instead of destination")
            if not self.rules and not self.categories:
                raise ValueError(
                    "organize requires extension rules or destination-to-category mappings"
                )
            if len(set(self.rules) | set(self.categories)) > 20:
                raise ValueError("organize supports at most 20 destination folders")
            if set(self.rules) & set(self.categories):
                raise ValueError(
                    "an organize destination cannot use both extensions and a category"
                )
            claimed_extensions: set[str] = set()
            expanded_rules = _expanded_organization_rules(self.rules, self.categories)
            for folder_name, extensions in expanded_rules.items():
                if (
                    not folder_name.strip()
                    or folder_name in {".", ".."}
                    or "/" in folder_name
                    or "\\" in folder_name
                    or not re.fullmatch(r"[A-Za-z0-9 _.-]{1,100}", folder_name)
                ):
                    raise ValueError("organize destination names must be simple folder names")
                if not extensions:
                    raise ValueError(
                        f"organize rule {folder_name!r} requires at least one extension"
                    )
                for extension in extensions:
                    normalized_extension = extension.strip().casefold()
                    if not normalized_extension.startswith("."):
                        normalized_extension = "." + normalized_extension
                    if not re.fullmatch(r"\.[a-z0-9]{1,20}", normalized_extension):
                        raise ValueError(f"invalid file extension: {extension}")
                    if normalized_extension in claimed_extensions:
                        raise ValueError(
                            f"file extension appears in more than one rule: {normalized_extension}"
                        )
                    claimed_extensions.add(normalized_extension)
        elif self.rules or self.categories:
            raise ValueError("rules and categories are only supported by organize")
        for field_name, value in (
            ("source", self.source),
            ("destination", self.destination),
        ):
            if value is None:
                continue
            if "*" in value or "?" in value:
                raise ValueError(
                    f"{field_name} must identify one exact path; wildcards are not supported"
                )
            unsupported = {
                match.casefold()
                for match in re.findall(r"\$\{[^}]+\}", value)
                if match.casefold() not in _ALLOWED_PATH_PLACEHOLDERS
            }
            if unsupported:
                raise ValueError(
                    f"{field_name} contains an unsupported path placeholder: "
                    + ", ".join(sorted(unsupported))
                )
        return self


class FileOperationOutput(BaseModel):
    operation: str
    source: str
    destination: str | None = None
    dry_run: bool
    moved: list[dict[str, str]] = Field(default_factory=list)
    created_directories: list[str] = Field(default_factory=list)


class FileOperationSkill(Skill):
    input_model = FileOperationInput
    output_model = FileOperationOutput

    def __init__(self, adapter: FileSystemAdapter) -> None:
        self.adapter = adapter
        self._prepared_organizations: dict[str, tuple[tuple[Path, Path], ...]] = {}
        self.manifest = manifest(
            skill_id="windows.file_operation",
            display_name="File Operation",
            description=(
                "Create text files; copy, move, rename, recycle, or open one exact "
                "approved path; or organize the direct files in one approved folder "
                "into named subfolders using explicit file-extension rules or standard "
                "images and text-files categories."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.HIGH,
            supports_rollback=True,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        source_path, destination_path = _resolve_operation_paths(args)
        source = str(source_path)
        if args.operation == "organize":
            return (
                PermissionScope(
                    capability="filesystem.read",
                    targets=(source,),
                    reason="Inspect direct files in this approved folder for extension matches.",
                ),
                PermissionScope(
                    capability="filesystem.write",
                    targets=(source,),
                    reason="Create named subfolders and move only the previewed matching files.",
                ),
            )
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
                PermissionScope(
                    capability="process.launch",
                    targets=(source,),
                    reason="Open the file in its registered visible application.",
                ),
            )
        if args.operation == "copy":
            assert destination_path
            return (
                PermissionScope(
                    capability="filesystem.read",
                    targets=(source,),
                    reason="Copy from this exact displayed source file.",
                ),
                PermissionScope(
                    capability="filesystem.write",
                    targets=(str(destination_path),),
                    reason="Copy to this exact displayed destination.",
                ),
            )
        targets = [source]
        if destination_path:
            targets.append(str(destination_path))
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
            "organize": RiskLevel.HIGH,
        }[operation]

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        source, destination_path = _resolve_operation_paths(args)
        if args.operation == "organize":
            moves = _organization_moves(
                source,
                _expanded_organization_rules(args.rules, args.categories),
            )
            self._prepared_organizations[call.idempotency_key] = moves
            prefix = "Preview only: " if args.dry_run else ""
            if not moves:
                return f"{prefix}Organize {source}: no direct files match the rules."
            lines = [f"{prefix}Organize {len(moves)} file(s) in {source}:"]
            lines.extend(
                f"- {source_path.name} to {destination}" for source_path, destination in moves
            )
            return "\n".join(lines)
        destination = f" to {destination_path}" if destination_path else ""
        prefix = "Preview only: " if args.dry_run else ""
        return f"{prefix}{_file_operation_label(args.operation)} {source}{destination}"

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        source, destination = _resolve_operation_paths(args)
        if args.operation == "organize":
            return await self._execute_organization(call, args, source, cancel_event)
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

    async def _execute_organization(
        self,
        call: SkillCall,
        args: FileOperationInput,
        source: Path,
        cancel_event: asyncio.Event,
    ) -> SkillResult:
        moves = self._prepared_organizations.pop(call.idempotency_key, None)
        if moves is None:
            moves = _organization_moves(
                source,
                _expanded_organization_rules(args.rules, args.categories),
            )
        for _, destination in moves:
            if destination.exists():
                raise FileExistsError(destination)
        data = FileOperationOutput(
            operation=args.operation,
            source=str(source),
            dry_run=args.dry_run,
            moved=[
                {"source": str(source_path), "destination": str(destination)}
                for source_path, destination in moves
            ],
            created_directories=[],
        ).model_dump()
        if args.dry_run:
            return SkillResult(
                status=ResultStatus.COMPLETED,
                summary=f"Previewed organization of {len(moves)} file(s); no files were changed.",
                data=data,
            )

        created_directories: list[str] = []
        moved: list[dict[str, str]] = []
        for source_path, destination in moves:
            if cancel_event.is_set():
                status = ResultStatus.PARTIAL if moved else ResultStatus.CANCELLED
                return SkillResult(
                    status=status,
                    summary=f"Organization stopped after moving {len(moved)} file(s).",
                    data={**data, "moved": moved, "created_directories": created_directories},
                )
            folder = destination.parent
            if not folder.exists():
                await self.adapter.create_directory(folder)
                created_directories.append(str(folder))
            await self.adapter.move(source_path, destination)
            moved.append({"source": str(source_path), "destination": str(destination)})
        data["moved"] = moved
        data["created_directories"] = created_directories
        rollback_token = json.dumps(
            {
                "operation": "move_back_many",
                "moves": [
                    {"source": item["destination"], "destination": item["source"]}
                    for item in reversed(moved)
                ],
            }
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Organized {len(moved)} file(s).",
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
        elif payload["operation"] == "move_back_many":
            restored = 0
            for move in payload["moves"]:
                source = Path(move["source"])
                if source.exists():
                    await self.adapter.move(source, Path(move["destination"]))
                    restored += 1
            summary = f"Restored {restored} organized file(s) to their original paths."
        else:
            raise ValueError("Unknown rollback token")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            data={"operation": "rollback"},
        )


def _file_operation_label(operation: str) -> str:
    if operation == "create_text":
        return "Create text file"
    if operation == "organize":
        return "Organize files"
    return operation.title()


def _organization_moves(
    source: Path,
    rules: dict[str, list[str]],
) -> tuple[tuple[Path, Path], ...]:
    if not source.is_dir():
        raise NotADirectoryError(source)
    destinations: dict[str, Path] = {}
    for folder_name, extensions in rules.items():
        destination_folder = (source / folder_name).resolve()
        if destination_folder.parent != source.resolve():
            raise ValueError("organize destinations must remain direct children of the source")
        for extension in extensions:
            normalized_extension = extension.strip().casefold()
            if not normalized_extension.startswith("."):
                normalized_extension = "." + normalized_extension
            destinations[normalized_extension] = destination_folder
    moves = []
    for path in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file():
            continue
        destination_folder = destinations.get(path.suffix.casefold())
        if destination_folder is not None:
            moves.append((path.resolve(), destination_folder / path.name))
    return tuple(moves)


def _expanded_organization_rules(
    rules: dict[str, list[str]],
    categories: dict[str, Literal["images", "text_files"]],
) -> dict[str, list[str]]:
    expanded = {folder: list(extensions) for folder, extensions in rules.items()}
    for folder, category in categories.items():
        expanded[folder] = list(_ORGANIZATION_CATEGORY_EXTENSIONS[category])
    return expanded


def _resolve_operation_paths(args: FileOperationInput) -> tuple[Path, Path | None]:
    source = resolve_user_path(args.source)
    destination = resolve_user_path(args.destination) if args.destination else None
    if args.operation == "rename":
        assert destination is not None
        # Rename changes only the leaf name. Constructing the target from the
        # source parent prevents an absolute or incorrectly resolved destination
        # from turning a rename into a move to another directory.
        destination = source.parent / destination.name
    return source, destination
