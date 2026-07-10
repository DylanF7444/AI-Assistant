from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

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
