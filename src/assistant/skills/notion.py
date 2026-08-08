from __future__ import annotations

import asyncio
import urllib.parse
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
from assistant.integrations.notion import NotionAdapter
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.skills.microsoft_graph_common import short_id


class NotionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "search",
        "get_page",
        "list_children",
        "get_data_source",
        "create_page",
        "append_content",
    ]
    query: str | None = Field(default=None, min_length=1, max_length=500)
    resource_id: str | None = Field(default=None, min_length=1, max_length=128)
    object_type: Literal["page", "data_source"] | None = None
    page_size: int = Field(default=25, ge=1, le=100)
    start_cursor: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    content: str | None = Field(default=None, min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def _operation_has_required_fields(self) -> NotionInput:
        if self.operation == "search" and not self.query:
            raise ValueError("search requires query")
        if self.operation != "search" and not self.resource_id:
            raise ValueError(f"{self.operation} requires resource_id")
        if self.operation == "create_page" and (not self.title or not self.content):
            raise ValueError("create_page requires title and content")
        if self.operation == "append_content" and not self.content:
            raise ValueError("append_content requires content")
        if self.operation not in {"create_page"} and self.title is not None:
            raise ValueError("title is only valid for create_page")
        if self.operation not in {"create_page", "append_content"} and self.content is not None:
            raise ValueError("content is only valid for page writes")
        return self


class NotionOutput(BaseModel):
    operation: str
    resources: list[dict[str, Any]] = Field(default_factory=list)
    next_page_token: str | None = None


class NotionSkill(Skill):
    input_model = NotionInput
    output_model = NotionOutput

    def __init__(self, adapter: NotionAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="integration.notion",
            display_name="Notion",
            description=(
                "Search shared Notion pages and data sources, inspect page or data-source "
                "metadata, read bounded child blocks, create one page under an exact parent, or "
                "append content to one exact page. Database writes and broad ingestion remain excluded."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="integration.read",
                    targets=(),
                    reason="Read approved Notion pages, data sources, and child blocks.",
                ),
                PermissionScope(
                    capability="network",
                    targets=("https://api.notion.com",),
                    reason="Connect to Notion's fixed API origin.",
                ),
            ),
            risk=RiskLevel.MEDIUM,
            safe_to_retry=False,
            background_mode=BackgroundMode.CAPABILITY_GATED,
        ).model_copy(update={"entry_point": f"{__name__}:NotionSkill"})

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.HIGH
            if self.validate(call.arguments).operation in {"create_page", "append_content"}
            else RiskLevel.LOW
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        write = args.operation in {"create_page", "append_content"}
        return (
            PermissionScope(
                capability="integration.write" if write else "integration.read",
                targets=(_permission_target(args),),
                reason=(
                    "Change this exact Notion page target."
                    if write
                    else "Allow Notion access for this exact read target."
                ),
            ),
            PermissionScope(
                capability="network",
                targets=("https://api.notion.com",),
                reason="Connect to Notion's fixed API origin.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "create_page":
            return f"Create Notion page {args.title!r} under parent {args.resource_id}"
        if args.operation == "append_content":
            return f"Append {len(args.content or '')} characters to Notion page {args.resource_id}"
        return f"Notion: {args.operation} on {_permission_target(args)}"

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="Notion action cancelled.")
        payload = await self._execute(args)
        resources = _resources(args.operation, payload)
        output = NotionOutput(
            operation=args.operation,
            resources=resources,
            next_page_token=_next_cursor(payload),
        )
        items = tuple(
            RequestResultItem(
                item_id=f"notion-{index}-{short_id(item)}",
                title=_title(item),
                description=_description(item),
                value={"resource_id": item.get("id"), "resource": item},
                kind="notion_resource",
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

    async def _execute(self, args: NotionInput) -> dict[str, Any]:
        if args.operation == "create_page":
            return await self.adapter.request(
                "POST",
                "/pages",
                body={
                    "parent": {"page_id": args.resource_id},
                    "properties": {
                        "title": {"title": [_rich_text(args.title or "")]},
                    },
                    "children": [_paragraph(args.content or "")],
                },
                safe_to_retry=False,
            )
        if args.operation == "append_content":
            return await self.adapter.request(
                "PATCH",
                f"/blocks/{urllib.parse.quote(args.resource_id or '', safe='')}/children",
                body={"children": [_paragraph(args.content or "")]},
                safe_to_retry=False,
            )
        if args.operation == "search":
            body: dict[str, Any] = {"query": args.query, "page_size": args.page_size}
            if args.start_cursor:
                body["start_cursor"] = args.start_cursor
            if args.object_type:
                body["filter"] = {"property": "object", "value": args.object_type}
            return await self.adapter.request("POST", "/search", body=body, safe_to_retry=True)
        if args.operation == "get_page":
            return await self.adapter.request(
                "GET", f"/pages/{urllib.parse.quote(args.resource_id or '', safe='')}"
            )
        if args.operation == "get_data_source":
            return await self.adapter.request(
                "GET", f"/data_sources/{urllib.parse.quote(args.resource_id or '', safe='')}"
            )
        return await self.adapter.request(
            "GET",
            f"/blocks/{urllib.parse.quote(args.resource_id or '', safe='')}/children",
            query={"page_size": args.page_size, "start_cursor": args.start_cursor},
        )


def _resources(operation: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if operation in {"search", "list_children", "append_content"}:
        values = payload.get("results")
        return (
            [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []
        )
    return [payload] if payload else []


def _next_cursor(payload: dict[str, Any]) -> str | None:
    cursor = payload.get("next_cursor")
    return cursor[:2048] if isinstance(cursor, str) and cursor else None


def _permission_target(args: NotionInput) -> str:
    if args.operation == "create_page":
        return f"notion:create-page:{args.resource_id}"
    if args.operation == "append_content":
        return f"notion:append-content:{args.resource_id}"
    if args.operation == "search":
        return f"notion:query:{args.query[:200] if args.query else '*'}"
    return f"notion:{args.operation}:{args.resource_id}"


def _rich_text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": {"content": value}}


def _paragraph(value: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [_rich_text(value)]},
    }


def _title(item: dict[str, Any]) -> str:
    title = item.get("title")
    if isinstance(title, list):
        parts = []
        for fragment in title:
            if isinstance(fragment, dict):
                text = fragment.get("plain_text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "".join(parts)[:500]
    properties = item.get("properties")
    if isinstance(properties, dict):
        for value in properties.values():
            if isinstance(value, dict) and isinstance(value.get("title"), list):
                nested = _title({"title": value["title"]})
                if nested:
                    return nested
    return str(item.get("id") or item.get("type") or "Notion resource")[:500]


def _description(item: dict[str, Any]) -> str | None:
    values = [item.get("object"), item.get("url"), item.get("last_edited_time")]
    return " • ".join(str(value)[:500] for value in values if value)[:2000] or None


def _summary(operation: str, count: int) -> str:
    if count:
        return f"I found {count} Notion result(s)."
    return "I couldn't find any Notion results."


def _display(operation: str, resources: list[dict[str, Any]]) -> str:
    if not resources:
        return "I couldn't find any Notion results."
    lines = [f"- {_title(item)}" for item in resources[:50]]
    return "\n".join(["Notion results:", *lines])
