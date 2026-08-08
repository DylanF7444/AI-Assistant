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
from assistant.integrations.slack import SlackAdapter
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.skills.microsoft_graph_common import short_id


class SlackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "search_messages",
        "list_conversations",
        "read_history",
        "read_thread",
        "post_message",
    ]
    query: str | None = Field(default=None, min_length=1, max_length=500)
    channel_id: str | None = Field(default=None, min_length=1, max_length=128)
    thread_ts: str | None = Field(default=None, min_length=1, max_length=80)
    max_results: int = Field(default=25, ge=1, le=50)
    page_token: str | None = Field(default=None, max_length=2048)
    text: str | None = Field(default=None, min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def _operation_has_required_fields(self) -> SlackInput:
        if self.operation == "search_messages" and not self.query:
            raise ValueError("search_messages requires query")
        if (
            self.operation in {"read_history", "read_thread", "post_message"}
            and not self.channel_id
        ):
            raise ValueError(f"{self.operation} requires channel_id")
        if self.operation == "read_thread" and not self.thread_ts:
            raise ValueError("read_thread requires thread_ts")
        if self.operation == "post_message" and not self.text:
            raise ValueError("post_message requires text")
        if self.operation != "post_message" and self.text is not None:
            raise ValueError("text is only valid for post_message")
        return self


class SlackOutput(BaseModel):
    operation: str
    resources: list[dict[str, Any]] = Field(default_factory=list)
    next_page_token: str | None = None


class SlackSkill(Skill):
    input_model = SlackInput
    output_model = SlackOutput

    def __init__(self, adapter: SlackAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="integration.slack",
            display_name="Slack",
            description=(
                "Search Slack messages, discover accessible conversations, and read bounded "
                "channel history or threads, and post one exact confirmed message to a channel "
                "or thread. Reactions, file uploads, and broad ingestion remain excluded."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="integration.read",
                    targets=(),
                    reason="Read approved Slack messages and conversation metadata.",
                ),
                PermissionScope(
                    capability="network",
                    targets=("https://slack.com",),
                    reason="Connect to Slack's fixed Web API origin.",
                ),
            ),
            risk=RiskLevel.MEDIUM,
            safe_to_retry=False,
            background_mode=BackgroundMode.CAPABILITY_GATED,
        ).model_copy(update={"entry_point": f"{__name__}:SlackSkill"})

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.HIGH
            if self.validate(call.arguments).operation == "post_message"
            else RiskLevel.LOW
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        write = args.operation == "post_message"
        return (
            PermissionScope(
                capability="messaging" if write else "integration.read",
                targets=(_permission_target(args),),
                reason=(
                    "Post this exact Slack message."
                    if write
                    else "Allow Slack access for this exact read target."
                ),
            ),
            PermissionScope(
                capability="network",
                targets=("https://slack.com",),
                reason="Connect to Slack's fixed Web API origin.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "post_message":
            destination = f"thread {args.thread_ts}" if args.thread_ts else "channel"
            return f"Post to Slack {destination} {args.channel_id}: {args.text}"
        return f"Slack: {args.operation} on {_permission_target(args)}"

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="Slack action cancelled.")
        payload = await self._execute(args)
        resources = _resources(args.operation, payload)
        output = SlackOutput(
            operation=args.operation,
            resources=resources,
            next_page_token=_next_cursor(payload),
        )
        items = tuple(
            RequestResultItem(
                item_id=f"slack-{index}-{short_id(item)}",
                title=_title(args.operation, item),
                description=_description(args.operation, item),
                value={
                    "resource": item,
                    "channel_id": item.get("channel", {}).get("id")
                    if isinstance(item.get("channel"), dict)
                    else args.channel_id,
                },
                kind="slack_message"
                if args.operation
                in {
                    "search_messages",
                    "read_history",
                    "read_thread",
                    "post_message",
                }
                else "slack_conversation",
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

    async def _execute(self, args: SlackInput) -> dict[str, Any]:
        if args.operation == "post_message":
            body = {"channel": args.channel_id, "text": args.text}
            if args.thread_ts:
                body["thread_ts"] = args.thread_ts
            return await self.adapter.request("POST", "/chat.postMessage", body=body)
        if args.operation == "search_messages":
            return await self.adapter.request(
                "GET",
                "/search.messages",
                query={
                    "query": args.query,
                    "count": args.max_results,
                    "cursor": args.page_token,
                    "sort": "timestamp",
                    "sort_dir": "desc",
                },
            )
        if args.operation == "list_conversations":
            return await self.adapter.request(
                "GET",
                "/conversations.list",
                query={
                    "types": "public_channel,private_channel,mpim,im",
                    "exclude_archived": True,
                    "limit": args.max_results,
                    "cursor": args.page_token,
                },
            )
        path = (
            "/conversations.history"
            if args.operation == "read_history"
            else "/conversations.replies"
        )
        query = {
            "channel": args.channel_id,
            "limit": args.max_results,
            "cursor": args.page_token,
        }
        if args.operation == "read_thread":
            query["ts"] = args.thread_ts
        return await self.adapter.request("GET", path, query=query)


def _resources(operation: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if operation == "post_message":
        message = payload.get("message")
        return [message if isinstance(message, dict) else payload]
    if operation == "search_messages":
        messages = payload.get("messages")
        return (
            [item for item in messages.get("matches", []) if isinstance(item, dict)]
            if isinstance(messages, dict)
            else []
        )
    key = "channels" if operation == "list_conversations" else "messages"
    values = payload.get(key)
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _next_cursor(payload: dict[str, Any]) -> str | None:
    metadata = payload.get("response_metadata")
    if not isinstance(metadata, dict):
        return None
    cursor = metadata.get("next_cursor")
    return cursor[:2048] if isinstance(cursor, str) and cursor else None


def _permission_target(args: SlackInput) -> str:
    if args.operation == "post_message":
        return f"slack:post:{args.channel_id}:{args.thread_ts or 'channel'}"
    if args.operation == "search_messages":
        return f"slack:query:{args.query[:200] if args.query else '*'}"
    if args.operation == "list_conversations":
        return "slack:conversations"
    if args.operation == "read_thread":
        return f"slack:thread:{args.channel_id}:{args.thread_ts}"
    return f"slack:channel:{args.channel_id}"


def _title(operation: str, item: dict[str, Any]) -> str:
    if operation == "list_conversations":
        return str(item.get("name") or item.get("id") or "Slack conversation")[:500]
    return str(item.get("text") or item.get("ts") or "Slack message")[:500]


def _description(operation: str, item: dict[str, Any]) -> str | None:
    if operation == "list_conversations":
        return (
            str(item.get("purpose", {}).get("value") or item.get("topic", {}).get("value") or "")[
                :2000
            ]
            or None
        )
    channel = item.get("channel")
    channel_name = channel.get("name") if isinstance(channel, dict) else None
    values = [channel_name, item.get("username") or item.get("user"), item.get("ts")]
    return " • ".join(str(value)[:500] for value in values if value)[:2000] or None


def _summary(operation: str, count: int) -> str:
    if count:
        return f"I found {count} Slack result(s)."
    return "I couldn't find any Slack results."


def _display(operation: str, resources: list[dict[str, Any]]) -> str:
    if not resources:
        return "I couldn't find any Slack results."
    heading = "Slack conversations:" if operation == "list_conversations" else "Slack messages:"
    lines = [f"- {_title(operation, item)}" for item in resources[:50]]
    return "\n".join([heading, *lines])
