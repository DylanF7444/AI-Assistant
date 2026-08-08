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

CALENDAR_FIELDS = "id,subject,bodyPreview,start,end,location,organizer,attendees,isAllDay,webLink"


class OutlookCalendarInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "search_events",
        "list_events",
        "get_event",
        "create_event",
        "update_event",
        "delete_event",
    ]
    query: str | None = Field(default=None, min_length=1, max_length=500)
    event_id: str | None = Field(default=None, min_length=1, max_length=1024)
    time_min: str | None = Field(default=None, min_length=1, max_length=80)
    time_max: str | None = Field(default=None, min_length=1, max_length=80)
    max_results: int = Field(default=25, ge=1, le=50)
    page_token: str | None = Field(default=None, max_length=2048)
    subject: str | None = Field(default=None, min_length=1, max_length=500)
    location: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _operation_has_required_fields(self) -> OutlookCalendarInput:
        if self.operation == "search_events" and not self.query:
            raise ValueError("search_events requires query")
        if self.operation == "get_event" and not self.event_id:
            raise ValueError("get_event requires event_id")
        if self.operation in {"update_event", "delete_event"} and not self.event_id:
            raise ValueError(f"{self.operation} requires event_id")
        if self.operation == "create_event" and not self.subject:
            raise ValueError("create_event requires subject")
        if bool(self.time_min) != bool(self.time_max):
            raise ValueError("time_min and time_max must be provided together")
        if self.operation in {"create_event", "update_event"} and not (
            self.subject or (self.time_min and self.time_max) or self.location
        ):
            raise ValueError(f"{self.operation} requires at least one event field")
        return self


class OutlookCalendarOutput(BaseModel):
    operation: str
    events: list[dict[str, Any]] = Field(default_factory=list)
    next_page_token: str | None = None


class OutlookCalendarSkill(Skill):
    input_model = OutlookCalendarInput
    output_model = OutlookCalendarOutput

    def __init__(self, adapter: MicrosoftGraphAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="integration.outlook_calendar",
            display_name="Outlook Calendar",
            description=(
                "Search and inspect Microsoft Outlook calendar events through Microsoft "
                "Graph. Create, update, and delete one exact event after presenting its target. "
                "Bulk changes and meeting responses remain excluded."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="integration.read",
                    targets=(),
                    reason="Read approved Outlook calendar events.",
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
        ).model_copy(update={"entry_point": f"{__name__}:OutlookCalendarSkill"})

    def risk_for(self, call: SkillCall) -> RiskLevel:
        operation = self.validate(call.arguments).operation
        return (
            RiskLevel.HIGH
            if operation in {"create_event", "update_event", "delete_event"}
            else RiskLevel.LOW
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        write = args.operation in {"create_event", "update_event", "delete_event"}
        return (
            PermissionScope(
                capability="integration.write" if write else "integration.read",
                targets=(_permission_target(args),),
                reason=(
                    "Change this exact Outlook calendar event."
                    if write
                    else "Allow Outlook Calendar access for this exact read target."
                ),
            ),
            PermissionScope(
                capability="network",
                targets=(GRAPH_API_ROOT.split("/v1.0")[0], AUTHORITY_ROOT),
                reason="Connect to Microsoft Graph and refresh its OAuth token.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "create_event":
            return f"Create Outlook event {args.subject!r} from {args.time_min} to {args.time_max}"
        if args.operation == "update_event":
            return f"Update Outlook event {args.event_id} with only the supplied fields"
        if args.operation == "delete_event":
            return f"Delete the exact Outlook event {args.event_id}"
        return f"Outlook Calendar: {args.operation} on {_permission_target(args)}"

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(
                status=ResultStatus.CANCELLED,
                summary="Outlook Calendar action cancelled.",
            )
        payload = await self._execute(args)
        events = resources_from_payload(payload)
        output = OutlookCalendarOutput(
            operation=args.operation,
            events=events,
            next_page_token=continuation_token(payload),
        )
        items = tuple(
            RequestResultItem(
                item_id=f"outlook-calendar-{index}-{short_id(item)}",
                title=str(item.get("subject") or "(no subject)")[:500],
                description=_event_description(item),
                value={"event_id": item.get("id"), "resource": item},
                kind="calendar_event",
                sensitive=True,
            )
            for index, item in enumerate(events, 1)
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=_summary(args.operation, len(events)),
            display_text=_display(args.operation, events),
            data=output.model_dump(mode="json"),
            items=items or None,
            sensitive=True,
        )

    async def _execute(self, args: OutlookCalendarInput) -> dict[str, Any]:
        if args.operation == "create_event":
            return await self.adapter.request("POST", "/me/events", body=_event_body(args))
        if args.operation == "update_event":
            return await self.adapter.request(
                "PATCH",
                f"/me/events/{quote_id(args.event_id or '')}",
                body=_event_body(args),
            )
        if args.operation == "delete_event":
            await self.adapter.request("DELETE", f"/me/events/{quote_id(args.event_id or '')}")
            return {"id": args.event_id, "deleted": True, "subject": "Deleted event"}
        if args.operation == "get_event":
            return await self.adapter.request(
                "GET",
                f"/me/events/{quote_id(args.event_id or '')}",
                query={"$select": CALENDAR_FIELDS},
            )
        path = "/me/calendarView" if args.time_min and args.time_max else "/me/events"
        return await self.adapter.request(
            "GET",
            path,
            query={
                "$select": CALENDAR_FIELDS,
                "$top": args.max_results,
                "$orderby": "start/dateTime",
                "$skiptoken": args.page_token,
                "$filter": _query_filter(args.query),
                "startDateTime": args.time_min if path.endswith("calendarView") else None,
                "endDateTime": args.time_max if path.endswith("calendarView") else None,
            },
        )


def _query_filter(query: str | None) -> str | None:
    if not query:
        return None
    return f"contains(subject,'{odata_literal(query)}')"


def _permission_target(args: OutlookCalendarInput) -> str:
    if args.operation == "create_event":
        return f"outlook-calendar:create:{args.time_min or '*'}"
    if args.operation in {"update_event", "delete_event"}:
        return f"outlook-calendar:{args.operation}:{args.event_id}"
    if args.operation == "get_event":
        return f"outlook-calendar:event:{args.event_id}"
    query = args.query[:200] if args.query else "*"
    window = f"{args.time_min or '*'}..{args.time_max or '*'}"
    return f"outlook-calendar:{window}:{query}"


def _event_body(args: OutlookCalendarInput) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if args.subject is not None:
        body["subject"] = args.subject
    if args.time_min and args.time_max:
        body["start"] = {"dateTime": args.time_min, "timeZone": "UTC"}
        body["end"] = {"dateTime": args.time_max, "timeZone": "UTC"}
    if args.location is not None:
        body["location"] = {"displayName": args.location}
    return body


def _event_description(item: dict[str, Any]) -> str | None:
    start = item.get("start")
    end = item.get("end")
    values = []
    if isinstance(start, dict):
        values.append(start.get("dateTime") or start.get("date"))
    if isinstance(end, dict):
        values.append(end.get("dateTime") or end.get("date"))
    location = item.get("location")
    if isinstance(location, dict):
        values.append(location.get("displayName"))
    preview = item.get("bodyPreview")
    values.append(preview)
    detail = " • ".join(str(value)[:500] for value in values if value)
    return detail[:2000] if detail else None


def _summary(operation: str, count: int) -> str:
    if count:
        return f"I found {count} Outlook calendar event(s)."
    return "I couldn't find any Outlook calendar events."


def _display(operation: str, events: list[dict[str, Any]]) -> str:
    if not events:
        return "I couldn't find any Outlook calendar events."
    heading = "Outlook calendar event:" if operation == "get_event" else "Outlook calendar events:"
    lines = []
    for item in events[:50]:
        title = str(item.get("subject") or "(no subject)")[:500]
        detail = _event_description(item)
        lines.append(f"- {title}" + (f"\n  {detail}" if detail else ""))
    return "\n".join([heading, *lines])
