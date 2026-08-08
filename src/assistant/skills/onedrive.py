from __future__ import annotations

import asyncio
import mimetypes
import urllib.parse
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
from assistant.integrations.microsoft_graph import (
    AUTHORITY_ROOT,
    GRAPH_API_ROOT,
    MicrosoftGraphAdapter,
    odata_literal,
    quote_id,
)
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.skills.microsoft_graph_common import (
    continuation_token,
    resources_from_payload,
    short_id,
)
from assistant.windows.paths import resolve_user_path

DRIVE_FIELDS = (
    "id,name,createdDateTime,lastModifiedDateTime,size,file,folder,webUrl,parentReference"
)


class OneDriveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["search", "list_children", "get_metadata", "download", "upload"]
    query: str | None = Field(default=None, min_length=1, max_length=500)
    file_id: str = Field(default="root", min_length=1, max_length=1024)
    max_results: int = Field(default=25, ge=1, le=100)
    page_token: str | None = Field(default=None, max_length=2048)
    local_path: str | None = Field(default=None, min_length=1, max_length=32_768)
    name: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _operation_has_required_fields(self) -> OneDriveInput:
        if self.operation == "search" and not self.query:
            raise ValueError("search requires query")
        if self.operation in {"get_metadata", "download"} and not self.file_id:
            raise ValueError(f"{self.operation} requires file_id")
        if self.operation in {"download", "upload"} and not self.local_path:
            raise ValueError(f"{self.operation} requires local_path")
        if self.operation != "upload" and self.name is not None:
            raise ValueError("name is only valid for upload")
        return self


class OneDriveOutput(BaseModel):
    operation: str
    resources: list[dict[str, Any]] = Field(default_factory=list)
    next_page_token: str | None = None


class OneDriveSkill(Skill):
    input_model = OneDriveInput
    output_model = OneDriveOutput

    def __init__(self, adapter: MicrosoftGraphAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="integration.onedrive",
            display_name="OneDrive",
            description=(
                "Search OneDrive files and folders, list folder children, and inspect file "
                "metadata, download an exact file to an exact new local path, or upload one "
                "explicit local file. Existing local and remote files are never overwritten."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="integration.read",
                    targets=(),
                    reason="Read approved OneDrive file and folder metadata.",
                ),
                PermissionScope(
                    capability="network",
                    targets=(GRAPH_API_ROOT.split("/v1.0")[0], AUTHORITY_ROOT),
                    reason="Connect to Microsoft Graph and refresh its OAuth token.",
                ),
            ),
            risk=RiskLevel.MEDIUM,
            safe_to_retry=False,
            background_mode=BackgroundMode.CAPABILITY_GATED,
        ).model_copy(update={"entry_point": f"{__name__}:OneDriveSkill"})

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.HIGH
            if self.validate(call.arguments).operation in {"download", "upload"}
            else RiskLevel.LOW
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        scopes = []
        if args.operation == "download":
            scopes.append(
                PermissionScope(
                    capability="filesystem.write",
                    targets=(str(resolve_user_path(args.local_path or "")),),
                    reason="Create this exact local file from the selected OneDrive item.",
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
                targets=(_permission_target(args),),
                reason=(
                    "Upload one file to this exact OneDrive target."
                    if args.operation == "upload"
                    else "Allow OneDrive access for this exact target."
                ),
            )
        )
        scopes.append(
            PermissionScope(
                capability="network",
                targets=(GRAPH_API_ROOT.split("/v1.0")[0], AUTHORITY_ROOT),
                reason="Connect to Microsoft Graph and refresh its OAuth token.",
            )
        )
        return tuple(scopes)

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "download":
            return f"Download OneDrive file {args.file_id} to new local file {args.local_path}"
        if args.operation == "upload":
            return f"Upload local file {args.local_path} to OneDrive folder {args.file_id}"
        return f"OneDrive: {args.operation} on {_permission_target(args)}"

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="OneDrive action cancelled.")
        payload = await self._execute(args)
        resources = resources_from_payload(payload)
        output = OneDriveOutput(
            operation=args.operation,
            resources=resources,
            next_page_token=continuation_token(payload),
        )
        items = tuple(
            RequestResultItem(
                item_id=f"onedrive-{index}-{short_id(item)}",
                title=str(item.get("name") or item.get("id") or "OneDrive item")[:500],
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

    async def _execute(self, args: OneDriveInput) -> dict[str, Any]:
        if args.operation == "download":
            metadata = await self.adapter.request(
                "GET",
                f"/me/drive/items/{quote_id(args.file_id)}",
                query={"$select": "id,name,size,@microsoft.graph.downloadUrl"},
            )
            download_url = metadata.get("@microsoft.graph.downloadUrl")
            if not isinstance(download_url, str):
                raise ValueError("OneDrive did not return a bounded download URL")
            payload = await self.adapter.download_url(download_url, max_bytes=25 * 1024 * 1024)
            destination = resolve_user_path(args.local_path or "").resolve()
            await asyncio.to_thread(_write_new_file, destination, payload)
            return {**metadata, "localPath": str(destination), "size": len(payload)}
        if args.operation == "upload":
            source = resolve_user_path(args.local_path or "").resolve()
            payload = await asyncio.to_thread(_read_bounded_file, source)
            remote_name = args.name or source.name
            parent = "root" if args.file_id == "root" else f"items/{quote_id(args.file_id)}"
            encoded_name = urllib.parse.quote(remote_name, safe="")
            return await self.adapter.upload_bytes(
                f"/me/drive/{parent}:/{encoded_name}:/content",
                payload,
                content_type=mimetypes.guess_type(remote_name)[0] or "application/octet-stream",
            )
        if args.operation == "search":
            term = urllib.parse.quote(odata_literal(args.query or ""), safe="")
            path = f"/me/drive/root/search(q='{term}')"
        elif args.operation == "get_metadata":
            path = (
                "/me/drive/root"
                if args.file_id == "root"
                else f"/me/drive/items/{quote_id(args.file_id)}"
            )
        elif args.file_id == "root":
            path = "/me/drive/root/children"
        else:
            path = f"/me/drive/items/{quote_id(args.file_id)}/children"
        query = {"$select": DRIVE_FIELDS}
        if args.operation != "get_metadata":
            query.update(
                {
                    "$top": args.max_results,
                    "$skiptoken": args.page_token,
                }
            )
        return await self.adapter.request("GET", path, query=query)


def _permission_target(args: OneDriveInput) -> str:
    if args.operation == "download":
        return f"onedrive:download:{args.file_id}"
    if args.operation == "upload":
        return f"onedrive:upload:{args.file_id}:{args.name or Path(args.local_path or '').name}"
    if args.operation == "search":
        return f"onedrive:query:{args.query[:200] if args.query else '*'}"
    return f"onedrive:file:{args.file_id}"


def _item_description(item: dict[str, Any]) -> str | None:
    values = [item.get("size"), item.get("lastModifiedDateTime"), item.get("webUrl")]
    file_info = item.get("file")
    folder_info = item.get("folder")
    if isinstance(file_info, dict) and file_info.get("mimeType"):
        values.insert(0, file_info.get("mimeType"))
    elif isinstance(folder_info, dict):
        values.insert(0, "folder")
    detail = " • ".join(str(value)[:500] for value in values if value is not None)
    return detail[:2000] if detail else None


def _summary(operation: str, count: int) -> str:
    if count:
        return f"I found {count} OneDrive item(s)."
    return "I couldn't find any OneDrive items."


def _display(operation: str, resources: list[dict[str, Any]]) -> str:
    if not resources:
        return "I couldn't find any OneDrive items."
    heading = "OneDrive item:" if operation == "get_metadata" else "OneDrive items:"
    lines = []
    for item in resources[:50]:
        name = str(item.get("name") or item.get("id") or "OneDrive item")[:500]
        detail = _item_description(item)
        lines.append(f"- {name}" + (f"\n  {detail}" if detail else ""))
    return "\n".join([heading, *lines])


def _read_bounded_file(path: Path) -> bytes:
    if not path.is_file():
        raise ValueError(f"Upload source does not exist: {path}")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("OneDrive simple upload source exceeds the 4 MB safety limit")
    return path.read_bytes()


def _write_new_file(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir():
        raise ValueError(f"Download destination folder does not exist: {path.parent}")
    with path.open("xb") as handle:
        handle.write(payload)
