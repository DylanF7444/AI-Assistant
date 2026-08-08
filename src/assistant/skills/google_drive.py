from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    RequestResultItem,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.integrations.google_drive import (
    API_ROOT,
    TOKEN_URL,
    GoogleDriveAdapter,
    drive_query_literal,
    quote_id,
)
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.windows.paths import resolve_user_path

READ_OPERATIONS = {"search", "list_children", "get_metadata"}


class GoogleDriveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["search", "list_children", "get_metadata", "download", "upload"]
    file_id: str | None = Field(default=None, min_length=1, max_length=1024)
    query: str | None = Field(default=None, min_length=1, max_length=500)
    max_results: int = Field(default=25, ge=1, le=100)
    page_token: str | None = Field(default=None, max_length=2048)
    order_by: Literal["name", "modifiedTime", "createdTime"] = "modifiedTime"
    local_path: str | None = Field(default=None, min_length=1, max_length=32_768)
    name: str | None = Field(default=None, min_length=1, max_length=500)
    parent_id: str | None = Field(default=None, min_length=1, max_length=1024)
    mime_type: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _operation_has_required_fields(self) -> GoogleDriveInput:
        if self.operation == "search" and not self.query:
            raise ValueError("search requires query")
        if self.operation in {"list_children", "get_metadata", "download"} and not self.file_id:
            raise ValueError(f"{self.operation} requires file_id")
        if self.operation in {"download", "upload"} and not self.local_path:
            raise ValueError(f"{self.operation} requires local_path")
        if self.operation not in {"upload"} and any(
            value is not None for value in (self.name, self.parent_id, self.mime_type)
        ):
            raise ValueError("name, parent_id, and mime_type are only valid for upload")
        if self.mime_type and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*", self.mime_type
        ):
            raise ValueError("mime_type must be a safe media type")
        return self


class GoogleDriveOutput(BaseModel):
    operation: str
    resources: list[dict[str, Any]] = Field(default_factory=list)
    next_page_token: str | None = None


class GoogleDriveSkill(Skill):
    input_model = GoogleDriveInput
    output_model = GoogleDriveOutput

    def __init__(self, adapter: GoogleDriveAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="integration.google_drive",
            display_name="Google Drive",
            description=(
                "Search Google Drive files and folders, list folder contents, and inspect "
                "metadata, download an exact file to an exact new local path, or upload one "
                "explicit local file. Existing remote and local files are never overwritten."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="integration.read",
                    targets=(),
                    reason="Read approved Google Drive file and folder metadata.",
                ),
                PermissionScope(
                    capability="network",
                    targets=(API_ROOT.split("/v3")[0], TOKEN_URL.rsplit("/", 1)[0]),
                    reason="Connect to Google Drive and refresh its OAuth token.",
                ),
            ),
            risk=RiskLevel.MEDIUM,
            safe_to_retry=False,
            background_mode=BackgroundMode.CAPABILITY_GATED,
        ).model_copy(update={"entry_point": f"{__name__}:GoogleDriveSkill"})

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.HIGH
            if self.validate(call.arguments).operation in {"download", "upload"}
            else RiskLevel.LOW
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        target = _permission_target(args)
        scopes = []
        if args.operation == "download":
            scopes.append(
                PermissionScope(
                    capability="filesystem.write",
                    targets=(str(resolve_user_path(args.local_path or "")),),
                    reason="Create this exact local file from the selected Drive item.",
                )
            )
        elif args.operation == "upload":
            scopes.append(
                PermissionScope(
                    capability="filesystem.content.read",
                    targets=(str(resolve_user_path(args.local_path or "")),),
                    reason="Read this exact local file for upload.",
                )
            )
        scopes.append(
            PermissionScope(
                capability="integration.write"
                if args.operation == "upload"
                else "integration.read",
                targets=(target,),
                reason=(
                    "Upload one file to this exact Google Drive target."
                    if args.operation == "upload"
                    else "Allow Google Drive access for this exact target."
                ),
            )
        )
        scopes.append(
            PermissionScope(
                capability="network",
                targets=(API_ROOT.split("/v3")[0], TOKEN_URL.rsplit("/", 1)[0]),
                reason="Connect to Google Drive and refresh its OAuth token.",
            )
        )
        return tuple(scopes)

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "download":
            return f"Download Google Drive file {args.file_id} to new local file {args.local_path}"
        if args.operation == "upload":
            return f"Upload local file {args.local_path} to Google Drive parent {args.parent_id or 'root'}"
        return f"Google Drive: {args.operation} on {_permission_target(args)}"

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(
                status=ResultStatus.CANCELLED,
                summary="Google Drive action cancelled.",
            )
        payload = await self._execute(args)
        resources = _resources(args.operation, payload)
        output = GoogleDriveOutput(
            operation=args.operation,
            resources=resources,
            next_page_token=_string(payload.get("nextPageToken")),
        )
        items = tuple(
            RequestResultItem(
                item_id=f"gdrive-{index}-{_short_id(item)}",
                title=str(item.get("name") or item.get("id") or f"Drive item {index}")[:500],
                description=_item_description(item),
                value={"file_id": item.get("id"), "resource": item},
                kind="cloud_file",
                sensitive=True,
            )
            for index, item in enumerate(resources, 1)
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=_summary(args.operation, len(resources)),
            display_text=_display(args.operation, resources),
            data=output.model_dump(mode="json"),
            items=items or None,
            sensitive=True,
        )

    async def _execute(
        self,
        args: GoogleDriveInput,
    ) -> dict[str, Any]:
        if args.operation == "download":
            payload = await self.adapter.download(args.file_id or "", max_bytes=25 * 1024 * 1024)
            destination = resolve_user_path(args.local_path or "").resolve()
            await asyncio.to_thread(_write_new_file, destination, payload)
            return {
                "id": args.file_id,
                "name": destination.name,
                "mimeType": mimetypes.guess_type(destination.name)[0],
                "localPath": str(destination),
                "size": len(payload),
            }
        if args.operation == "upload":
            source = resolve_user_path(args.local_path or "").resolve()
            payload = await asyncio.to_thread(_read_bounded_file, source)
            return await self.adapter.upload(
                name=args.name or source.name,
                payload=payload,
                parent_id=args.parent_id,
                mime_type=args.mime_type,
            )
        if args.operation == "search":
            literal = drive_query_literal(args.query or "")
            query = (
                f"trashed = false and (name contains '{literal}' or fullText contains '{literal}')"
            )
            return await self.adapter.request(
                "GET",
                "/files",
                query={
                    "q": query,
                    "spaces": "drive",
                    "pageSize": args.max_results,
                    "pageToken": args.page_token,
                    "orderBy": f"{args.order_by} desc" if args.order_by != "name" else "name",
                    "fields": f"nextPageToken,files({FILE_FIELDS})",
                    "includeItemsFromAllDrives": True,
                    "supportsAllDrives": True,
                },
            )
        if args.operation == "list_children":
            parent = drive_query_literal(args.file_id or "root")
            return await self.adapter.request(
                "GET",
                "/files",
                query={
                    "q": f"'{parent}' in parents and trashed = false",
                    "spaces": "drive",
                    "pageSize": args.max_results,
                    "pageToken": args.page_token,
                    "orderBy": "name",
                    "fields": f"nextPageToken,files({FILE_FIELDS})",
                    "includeItemsFromAllDrives": True,
                    "supportsAllDrives": True,
                },
            )
        return await self.adapter.request(
            "GET",
            f"/files/{quote_id(args.file_id or '')}",
            query={
                "fields": FILE_FIELDS,
                "supportsAllDrives": True,
            },
        )


FILE_FIELDS = (
    "id,name,mimeType,size,createdTime,modifiedTime,parents,webViewLink,description,"
    "trashed,driveId,md5Checksum"
)


def _permission_target(args: GoogleDriveInput) -> str:
    if args.operation == "download":
        return f"google_drive:download:{args.file_id}"
    if args.operation == "upload":
        return f"google_drive:upload:{args.parent_id or 'root'}:{args.name or Path(args.local_path or '').name}"
    if args.operation == "search":
        return f"google_drive:query:{args.query or ''}"
    return f"google_drive:file:{args.file_id or 'root'}"


def _resources(operation: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if operation in {"search", "list_children"}:
        return [item for item in payload.get("files", []) if isinstance(item, dict)]
    return [payload] if payload else []


def _item_description(item: dict[str, Any]) -> str | None:
    values: list[str] = []
    mime_type = item.get("mimeType")
    if isinstance(mime_type, str) and mime_type:
        values.append(mime_type[:200])
    modified = item.get("modifiedTime")
    if isinstance(modified, str) and modified:
        values.append(f"modified {modified[:80]}")
    description = item.get("description")
    if isinstance(description, str) and description:
        values.append(description[:1000])
    return " • ".join(values) or None


def _short_id(item: dict[str, Any]) -> str:
    raw = str(item.get("id") or item.get("name") or "file").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:10]


def _summary(operation: str, count: int) -> str:
    if count:
        return f"I found {count} Google Drive item(s)."
    return "I couldn't find any matching Google Drive items."


def _display(operation: str, resources: list[dict[str, Any]]) -> str:
    if not resources:
        return "No matching Google Drive items were found."
    lines = []
    for item in resources[:50]:
        name = str(item.get("name") or item.get("id") or "Drive item")[:500]
        detail = _item_description(item)
        lines.append(f"- {name}" + (f"\n  {detail}" if detail else ""))
    return "\n".join(lines)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _read_bounded_file(path: Path) -> bytes:
    if not path.is_file():
        raise ValueError(f"Upload source does not exist: {path}")
    if path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError("Upload source exceeds the 25 MB safety limit")
    return path.read_bytes()


def _write_new_file(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir():
        raise ValueError(f"Download destination folder does not exist: {path.parent}")
    with path.open("xb") as handle:
        handle.write(payload)
