from __future__ import annotations

import asyncio
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
    TOKEN_URL,
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

MAIL_FIELDS = (
    "id,subject,from,receivedDateTime,bodyPreview,isRead,hasAttachments,webLink"
)


class OutlookMailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["search_messages", "list_messages", "get_message"]
    query: str | None = Field(default=None, min_length=1, max_length=500)
    message_id: str | None = Field(default=None, min_length=1, max_length=1024)
    folder_id: str = Field(default="inbox", min_length=1, max_length=256)
    max_results: int = Field(default=25, ge=1, le=50)
    page_token: str | None = Field(default=None, max_length=2048)
    unread_only: bool = False

    @model_validator(mode="after")
    def _operation_has_required_fields(self) -> OutlookMailInput:
        if self.operation == "search_messages" and not self.query:
            raise ValueError("search_messages requires query")
        if self.operation == "get_message" and not self.message_id:
            raise ValueError("get_message requires message_id")
        return self


class OutlookMailOutput(BaseModel):
    operation: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    next_page_token: str | None = None


class OutlookMailSkill(Skill):
    input_model = OutlookMailInput
    output_model = OutlookMailOutput

    def __init__(self, adapter: MicrosoftGraphAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="integration.outlook_mail",
            display_name="Outlook Mail",
            description=(
                "Search and inspect Microsoft Outlook messages through Microsoft Graph. "
                "This first slice is read-only; sending, drafting, moving, and deleting "
                "messages are intentionally excluded."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="integration.read",
                    targets=(),
                    reason="Read approved Outlook messages and previews.",
                ),
                PermissionScope(
                    capability="network",
                    targets=(GRAPH_API_ROOT.split("/v1.0")[0], AUTHORITY_ROOT),
                    reason="Connect to Microsoft Graph and refresh its OAuth token.",
                ),
            ),
            risk=RiskLevel.MEDIUM,
            safe_to_retry=True,
            background_mode=BackgroundMode.CAPABILITY_GATED,
        ).model_copy(update={"entry_point": f"{__name__}:OutlookMailSkill"})

    def risk_for(self, call: SkillCall) -> RiskLevel:
        self.validate(call.arguments)
        return RiskLevel.LOW

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return (
            PermissionScope(
                capability="integration.read",
                targets=(_permission_target(args),),
                reason="Allow Outlook Mail access for this exact read target.",
            ),
            PermissionScope(
                capability="network",
                targets=(GRAPH_API_ROOT.split("/v1.0")[0], AUTHORITY_ROOT),
                reason="Connect to Microsoft Graph and refresh its OAuth token.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        return f"Outlook Mail: {args.operation} on {_permission_target(args)}"

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="Outlook Mail action cancelled.")
        payload = await self._execute(args)
        messages = resources_from_payload(payload)
        output = OutlookMailOutput(
            operation=args.operation,
            messages=messages,
            next_page_token=continuation_token(payload),
        )
        items = tuple(
            RequestResultItem(
                item_id=f"outlook-mail-{index}-{short_id(item)}",
                title=str(item.get("subject") or "(no subject)")[:500],
                description=_message_description(item),
                value={"message_id": item.get("id"), "resource": item},
                kind="email",
                sensitive=True,
            )
            for index, item in enumerate(messages, 1)
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=_summary(args.operation, len(messages)),
            display_text=_display(args.operation, messages),
            data=output.model_dump(mode="json"),
            items=items or None,
            sensitive=True,
        )

    async def _execute(self, args: OutlookMailInput) -> dict[str, Any]:
        if args.operation == "get_message":
            return await self.adapter.request(
                "GET",
                f"/me/messages/{quote_id(args.message_id or '')}",
                query={"$select": MAIL_FIELDS},
            )
        path = f"/me/mailFolders/{quote_id(args.folder_id)}/messages"
        return await self.adapter.request(
            "GET",
            path,
            query={
                "$select": MAIL_FIELDS,
                "$top": args.max_results,
                "$orderby": "receivedDateTime desc",
                "$skiptoken": args.page_token,
                "$filter": _query_filter(args.query, unread_only=args.unread_only),
            },
        )


def _query_filter(query: str | None, *, unread_only: bool = False) -> str | None:
    filters = []
    if query:
        literal = odata_literal(query)
        filters.append(f"(contains(subject,'{literal}') or contains(bodyPreview,'{literal}'))")
    if unread_only:
        filters.append("isRead eq false")
    return " and ".join(filters) or None


def _permission_target(args: OutlookMailInput) -> str:
    if args.operation == "get_message":
        return f"outlook:message:{args.message_id}"
    query = args.query[:200] if args.query else ("unread" if args.unread_only else "*")
    return f"outlook:mail:{args.folder_id}:{query}"


def _message_description(item: dict[str, Any]) -> str | None:
    sender = item.get("from")
    sender_value = None
    if isinstance(sender, dict):
        address = sender.get("emailAddress")
        if isinstance(address, dict):
            sender_value = address.get("name") or address.get("address")
    preview = item.get("bodyPreview")
    values = [sender_value, item.get("receivedDateTime"), preview]
    detail = " • ".join(str(value)[:500] for value in values if value)
    return detail[:2000] if detail else None


def _summary(operation: str, count: int) -> str:
    if count:
        return f"I found {count} Outlook message(s)." if operation != "get_message" else "I found the Outlook message."
    return "I couldn't find any Outlook messages."


def _display(operation: str, messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "I couldn't find any Outlook messages."
    heading = "Outlook message:" if operation == "get_message" else "Outlook messages:"
    lines = []
    for item in messages[:50]:
        subject = str(item.get("subject") or "(no subject)")[:500]
        detail = _message_description(item)
        lines.append(f"- {subject}" + (f"\n  {detail}" if detail else ""))
    return "\n".join([heading, *lines])
