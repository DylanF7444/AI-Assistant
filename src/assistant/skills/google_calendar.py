from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    RequestResultItem,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.integrations.google_calendar import GoogleCalendarAdapter, quote_id
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest

READ_OPERATIONS = {
    "list_calendars",
    "get_calendar_list_entry",
    "get_calendar",
    "list_events",
    "get_event",
    "list_instances",
    "free_busy",
    "get_colors",
    "list_settings",
    "get_setting",
    "list_access_rules",
    "get_access_rule",
}
CRITICAL_OPERATIONS = {
    "clear_calendar",
    "delete_calendar",
    "transfer_calendar_ownership",
    "create_access_rule",
    "update_access_rule",
    "delete_access_rule",
}
EVENT_BODY_OPERATIONS = {"create_event", "import_event", "update_event"}


class GoogleCalendarInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "list_calendars",
        "get_calendar_list_entry",
        "add_calendar_to_list",
        "update_calendar_list_entry",
        "remove_calendar_from_list",
        "get_calendar",
        "create_calendar",
        "update_calendar",
        "clear_calendar",
        "delete_calendar",
        "transfer_calendar_ownership",
        "list_events",
        "get_event",
        "list_instances",
        "create_event",
        "import_event",
        "quick_add",
        "update_event",
        "delete_event",
        "move_event",
        "respond_to_event",
        "free_busy",
        "get_colors",
        "list_settings",
        "get_setting",
        "list_access_rules",
        "get_access_rule",
        "create_access_rule",
        "update_access_rule",
        "delete_access_rule",
    ]
    calendar_id: str = Field(default="primary", min_length=1, max_length=1024)
    event_id: str | None = Field(default=None, min_length=1, max_length=1024)
    destination_calendar_id: str | None = Field(default=None, min_length=1, max_length=1024)
    new_data_owner: str | None = Field(default=None, min_length=3, max_length=320)
    setting_id: str | None = Field(default=None, min_length=1, max_length=200)
    rule_id: str | None = Field(default=None, min_length=1, max_length=1024)
    query: str | None = Field(default=None, max_length=1000)
    i_cal_uid: str | None = Field(default=None, min_length=1, max_length=1024)
    time_min: datetime | None = None
    time_max: datetime | None = None
    time_zone: str | None = Field(default=None, min_length=1, max_length=100)
    max_results: int = Field(default=50, ge=1, le=250)
    page_token: str | None = Field(default=None, max_length=2048)
    sync_token: str | None = Field(default=None, max_length=4096)
    single_events: bool = True
    show_deleted: bool = False
    order_by: Literal["startTime", "updated"] | None = None
    summary: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=8000)
    location: str | None = Field(default=None, max_length=1000)
    start: datetime | date | None = None
    end: datetime | date | None = None
    time_zone_for_event: str | None = Field(default=None, min_length=1, max_length=100)
    recurrence: list[str] | None = Field(default=None, max_length=20)
    attendees: list[str] | None = Field(default=None, max_length=200)
    visibility: Literal["default", "public", "private", "confidential"] | None = None
    transparency: Literal["opaque", "transparent"] | None = None
    color_id: str | None = Field(default=None, max_length=20)
    reminder_minutes: list[int] | None = Field(default=None, max_length=5)
    add_google_meet: bool = False
    send_updates: Literal["all", "externalOnly", "none"] = "all"
    quick_add_text: str | None = Field(default=None, min_length=1, max_length=2000)
    response: Literal["accepted", "declined", "tentative", "needsAction"] | None = None
    calendar_description: str | None = Field(default=None, max_length=8000)
    calendar_location: str | None = Field(default=None, max_length=1000)
    summary_override: str | None = Field(default=None, max_length=1000)
    selected: bool | None = None
    hidden: bool | None = None
    background_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    foreground_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    scope_type: Literal["default", "user", "group", "domain"] | None = None
    scope_value: str | None = Field(default=None, max_length=1000)
    role: Literal["none", "freeBusyReader", "reader", "writer", "owner"] | None = None
    send_notifications: bool = True

    @field_validator("attendees")
    @classmethod
    def _attendees_look_like_email_addresses(cls, value: list[str] | None):
        if value is not None and any("@" not in item or len(item) > 320 for item in value):
            raise ValueError("Every attendee must be an email address")
        return value

    @field_validator("reminder_minutes")
    @classmethod
    def _reminders_are_in_google_range(cls, value: list[int] | None):
        if value is not None and any(item < 0 or item > 40320 for item in value):
            raise ValueError("Reminder minutes must be between 0 and 40320")
        return value

    @model_validator(mode="after")
    def _operation_has_required_fields(self):
        if self.time_min and self.time_max and self.time_min >= self.time_max:
            raise ValueError("time_min must be earlier than time_max")
        if self.order_by == "startTime" and not self.single_events:
            raise ValueError("startTime ordering requires single_events")
        if self.sync_token and any((self.query, self.time_min, self.time_max, self.order_by)):
            raise ValueError("sync_token cannot be combined with search or time filters")
        needs_event = {
            "get_event",
            "update_event",
            "delete_event",
            "move_event",
            "respond_to_event",
            "list_instances",
        }
        if self.operation in needs_event and not self.event_id:
            raise ValueError(f"{self.operation} requires event_id")
        if (
            self.operation in {"get_access_rule", "update_access_rule", "delete_access_rule"}
            and not self.rule_id
        ):
            raise ValueError(f"{self.operation} requires rule_id")
        if self.operation in {"create_access_rule", "update_access_rule"}:
            if not self.role:
                raise ValueError(f"{self.operation} requires role")
            if self.operation == "create_access_rule" and not self.scope_type:
                raise ValueError("create_access_rule requires scope_type")
            if self.scope_type in {"user", "group", "domain"} and not self.scope_value:
                raise ValueError(f"{self.scope_type} access rules require scope_value")
        if self.operation == "move_event" and not self.destination_calendar_id:
            raise ValueError("move_event requires destination_calendar_id")
        if self.operation == "transfer_calendar_ownership" and (
            not self.new_data_owner or "@" not in self.new_data_owner
        ):
            raise ValueError("transfer_calendar_ownership requires new_data_owner email")
        if self.operation == "get_setting" and not self.setting_id:
            raise ValueError("get_setting requires setting_id")
        if self.operation == "quick_add" and not self.quick_add_text:
            raise ValueError("quick_add requires quick_add_text")
        if self.operation == "respond_to_event" and not self.response:
            raise ValueError("respond_to_event requires response")
        if self.operation in {"create_calendar", "update_calendar"} and not self.summary:
            raise ValueError(f"{self.operation} requires summary")
        if self.operation in {"create_event", "import_event"} and (
            not self.summary or self.start is None or self.end is None
        ):
            raise ValueError(f"{self.operation} requires summary, start, and end")
        if self.operation == "import_event" and not self.i_cal_uid:
            raise ValueError("import_event requires i_cal_uid")
        if self.operation == "free_busy" and (self.time_min is None or self.time_max is None):
            raise ValueError("free_busy requires time_min and time_max")
        if self.operation == "update_event" and not any(
            value is not None
            for value in (
                self.summary,
                self.description,
                self.location,
                self.start,
                self.end,
                self.recurrence,
                self.attendees,
                self.visibility,
                self.transparency,
                self.color_id,
                self.reminder_minutes,
            )
        ):
            raise ValueError("update_event requires at least one changed field")
        if (
            isinstance(self.start, date)
            and not isinstance(self.start, datetime)
            and (not isinstance(self.end, date) or isinstance(self.end, datetime))
        ):
            raise ValueError("All-day event start and end must both be dates")
        if isinstance(self.start, datetime) and not isinstance(self.end, datetime):
            raise ValueError("Timed event start and end must both be date-times")
        if (
            isinstance(self.start, datetime)
            and self.start.tzinfo is None
            and not self.time_zone_for_event
        ):
            raise ValueError("Timed events need UTC offsets or time_zone_for_event")
        if (
            isinstance(self.end, datetime)
            and self.end.tzinfo is None
            and not self.time_zone_for_event
        ):
            raise ValueError("Timed events need UTC offsets or time_zone_for_event")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("Event start must be earlier than event end")
        return self


class GoogleCalendarOutput(BaseModel):
    operation: str
    resources: list[dict[str, Any]] = Field(default_factory=list)
    next_page_token: str | None = None
    next_sync_token: str | None = None


class GoogleCalendarSkill(Skill):
    input_model = GoogleCalendarInput
    output_model = GoogleCalendarOutput

    def __init__(self, adapter: GoogleCalendarAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="integration.google_calendar",
            display_name="Google Calendar",
            description=(
                "Read and manage Google calendars, events, recurring instances, "
                "availability, invitations, colors, settings, and sharing rules."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="integration.read",
                    targets=(),
                    reason="Read approved Google Calendar information.",
                ),
                PermissionScope(
                    capability="integration.write",
                    targets=(),
                    reason="Change approved Google Calendar information.",
                ),
                PermissionScope(
                    capability="network",
                    targets=("https://www.googleapis.com", "https://oauth2.googleapis.com"),
                    reason="Connect to Google Calendar and refresh its OAuth token.",
                ),
            ),
            risk=RiskLevel.HIGH,
            safe_to_retry=False,
            background_mode=BackgroundMode.CAPABILITY_GATED,
        ).model_copy(update={"entry_point": f"{__name__}:GoogleCalendarSkill"})

    def risk_for(self, call: SkillCall) -> RiskLevel:
        operation = self.validate(call.arguments).operation
        if operation in READ_OPERATIONS:
            return RiskLevel.LOW
        if operation in CRITICAL_OPERATIONS:
            return RiskLevel.CRITICAL
        if operation in {"create_event", "quick_add", "respond_to_event"}:
            return RiskLevel.MEDIUM
        return RiskLevel.HIGH

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        target = _permission_target(args)
        capability = (
            "integration.read" if args.operation in READ_OPERATIONS else "integration.write"
        )
        return (
            PermissionScope(
                capability=capability,
                targets=(target,),
                reason=f"Allow Google Calendar operation {args.operation} on this exact target.",
            ),
            PermissionScope(
                capability="network",
                targets=("https://www.googleapis.com", "https://oauth2.googleapis.com"),
                reason="Connect to Google Calendar and refresh its OAuth token.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        return f"Google Calendar: {args.operation} on {_permission_target(args)}"

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="Calendar action cancelled.")
        payload = await self._execute(args, idempotency_key=call.idempotency_key)
        resources = _resources(payload)
        output = GoogleCalendarOutput(
            operation=args.operation,
            resources=resources,
            next_page_token=_string(payload.get("nextPageToken")),
            next_sync_token=_string(payload.get("nextSyncToken")),
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=_summary(args.operation, len(resources)),
            display_text=_display(args, resources),
            data=output.model_dump(mode="json"),
            items=tuple(
                RequestResultItem(
                    item_id=f"gcal-{index}-{_short_id(item)}",
                    title=str(item.get("summary") or item.get("id") or f"Calendar item {index}")[
                        :500
                    ],
                    description=_item_description(item),
                    value={
                        "calendar_id": args.calendar_id,
                        "event_id": item.get("id"),
                        "resource": item,
                    },
                    kind="calendar_event" if "start" in item else "calendar_resource",
                    sensitive=True,
                )
                for index, item in enumerate(resources, 1)
            )
            or None,
            sensitive=True,
        )

    async def _execute(
        self,
        args: GoogleCalendarInput,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        calendar = quote_id(args.calendar_id)
        event = quote_id(args.event_id) if args.event_id else None
        operation = args.operation
        if operation == "list_calendars":
            return await self.adapter.request(
                "GET",
                "/users/me/calendarList",
                query={"maxResults": args.max_results, "pageToken": args.page_token},
            )
        if operation == "get_calendar_list_entry":
            return await self.adapter.request("GET", f"/users/me/calendarList/{calendar}")
        if operation == "add_calendar_to_list":
            return await self.adapter.request(
                "POST",
                "/users/me/calendarList",
                query={"colorRgbFormat": bool(args.background_color or args.foreground_color)},
                body={"id": args.calendar_id, **_calendar_list_body(args)},
            )
        if operation == "update_calendar_list_entry":
            return await self.adapter.request(
                "PATCH",
                f"/users/me/calendarList/{calendar}",
                query={"colorRgbFormat": bool(args.background_color or args.foreground_color)},
                body=_calendar_list_body(args),
            )
        if operation == "remove_calendar_from_list":
            return await self.adapter.request("DELETE", f"/users/me/calendarList/{calendar}")
        if operation == "get_calendar":
            return await self.adapter.request("GET", f"/calendars/{calendar}")
        if operation == "create_calendar":
            return await self.adapter.request("POST", "/calendars", body=_calendar_body(args))
        if operation == "update_calendar":
            return await self.adapter.request(
                "PATCH", f"/calendars/{calendar}", body=_calendar_body(args)
            )
        if operation == "clear_calendar":
            return await self.adapter.request("POST", f"/calendars/{calendar}/clear")
        if operation == "delete_calendar":
            return await self.adapter.request("DELETE", f"/calendars/{calendar}")
        if operation == "transfer_calendar_ownership":
            return await self.adapter.request(
                "POST",
                f"/calendars/{calendar}/transferOwnership",
                query={"newDataOwner": args.new_data_owner, "useAdminAccess": True},
            )
        if operation == "list_events":
            return await self.adapter.request(
                "GET", f"/calendars/{calendar}/events", query=_event_list_query(args)
            )
        if operation == "get_event":
            return await self.adapter.request("GET", f"/calendars/{calendar}/events/{event}")
        if operation == "list_instances":
            return await self.adapter.request(
                "GET",
                f"/calendars/{calendar}/events/{event}/instances",
                query={
                    "timeMin": _rfc3339(args.time_min),
                    "timeMax": _rfc3339(args.time_max),
                    "timeZone": args.time_zone,
                    "maxResults": args.max_results,
                    "pageToken": args.page_token,
                    "showDeleted": args.show_deleted,
                },
            )
        if operation in EVENT_BODY_OPERATIONS:
            method = {"create_event": "POST", "import_event": "POST", "update_event": "PATCH"}[
                operation
            ]
            suffix = (
                "/import"
                if operation == "import_event"
                else f"/{event}"
                if operation == "update_event"
                else ""
            )
            event_query = {
                "conferenceDataVersion": 1 if args.add_google_meet else None,
            }
            if operation != "import_event":
                event_query["sendUpdates"] = args.send_updates
            return await self.adapter.request(
                method,
                f"/calendars/{calendar}/events{suffix}",
                query=event_query,
                body=_event_body(args, call_id=idempotency_key),
            )
        if operation == "quick_add":
            return await self.adapter.request(
                "POST",
                f"/calendars/{calendar}/events/quickAdd",
                query={"text": args.quick_add_text, "sendUpdates": args.send_updates},
            )
        if operation == "delete_event":
            return await self.adapter.request(
                "DELETE",
                f"/calendars/{calendar}/events/{event}",
                query={"sendUpdates": args.send_updates},
            )
        if operation == "move_event":
            return await self.adapter.request(
                "POST",
                f"/calendars/{calendar}/events/{event}/move",
                query={
                    "destination": args.destination_calendar_id,
                    "sendUpdates": args.send_updates,
                },
            )
        if operation == "respond_to_event":
            current = await self.adapter.request("GET", f"/calendars/{calendar}/events/{event}")
            attendees = current.get("attendees")
            if not isinstance(attendees, list):
                raise ValueError("This event has no attendee invitation to answer")
            changed = False
            for attendee in attendees:
                if isinstance(attendee, dict) and attendee.get("self") is True:
                    attendee["responseStatus"] = args.response
                    changed = True
            if not changed:
                raise ValueError("Google did not identify this account as an attendee")
            return await self.adapter.request(
                "PATCH",
                f"/calendars/{calendar}/events/{event}",
                query={"sendUpdates": args.send_updates},
                body={"attendees": attendees},
            )
        if operation == "free_busy":
            calendars = [
                {"id": item.strip()}
                for item in (args.query or args.calendar_id).split(",")
                if item.strip()
            ]
            return await self.adapter.request(
                "POST",
                "/freeBusy",
                body={
                    "timeMin": _rfc3339(args.time_min),
                    "timeMax": _rfc3339(args.time_max),
                    "timeZone": args.time_zone,
                    "items": calendars,
                },
            )
        if operation == "get_colors":
            return await self.adapter.request("GET", "/colors")
        if operation == "list_settings":
            return await self.adapter.request(
                "GET",
                "/users/me/settings",
                query={"maxResults": args.max_results, "pageToken": args.page_token},
            )
        if operation == "get_setting":
            return await self.adapter.request(
                "GET", f"/users/me/settings/{quote_id(args.setting_id or '')}"
            )
        acl_root = f"/calendars/{calendar}/acl"
        if operation == "list_access_rules":
            return await self.adapter.request(
                "GET",
                acl_root,
                query={"maxResults": args.max_results, "pageToken": args.page_token},
            )
        if operation == "get_access_rule":
            return await self.adapter.request("GET", f"{acl_root}/{quote_id(args.rule_id or '')}")
        if operation == "create_access_rule":
            return await self.adapter.request(
                "POST",
                acl_root,
                query={"sendNotifications": args.send_notifications},
                body=_acl_body(args, include_scope=True),
            )
        if operation == "update_access_rule":
            return await self.adapter.request(
                "PATCH",
                f"{acl_root}/{quote_id(args.rule_id or '')}",
                query={"sendNotifications": args.send_notifications},
                body=_acl_body(args, include_scope=False),
            )
        if operation == "delete_access_rule":
            return await self.adapter.request(
                "DELETE", f"{acl_root}/{quote_id(args.rule_id or '')}"
            )
        raise ValueError(f"Unsupported Google Calendar operation: {operation}")


def _permission_target(args: GoogleCalendarInput) -> str:
    value = args.rule_id or args.event_id or args.destination_calendar_id or "*"
    return f"google-calendar:{args.calendar_id}:{value}"


def _calendar_body(args: GoogleCalendarInput) -> dict[str, Any]:
    return _without_none(
        {
            "summary": args.summary,
            "description": args.calendar_description,
            "location": args.calendar_location,
            "timeZone": args.time_zone,
        }
    )


def _event_body(args: GoogleCalendarInput, call_id: str | None) -> dict[str, Any]:
    body = _without_none(
        {
            "summary": args.summary,
            "description": args.description,
            "location": args.location,
            "start": _event_time(args.start, args.time_zone_for_event),
            "end": _event_time(args.end, args.time_zone_for_event),
            "recurrence": args.recurrence,
            "iCalUID": args.i_cal_uid,
            "attendees": (
                [{"email": address} for address in args.attendees]
                if args.attendees is not None
                else None
            ),
            "visibility": args.visibility,
            "transparency": args.transparency,
            "colorId": args.color_id,
            "reminders": (
                {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": minutes} for minutes in args.reminder_minutes
                    ],
                }
                if args.reminder_minutes is not None
                else None
            ),
        }
    )
    if args.add_google_meet:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": call_id or hashlib.sha256(repr(body).encode()).hexdigest()[:24],
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    return body


def _event_list_query(args: GoogleCalendarInput) -> dict[str, Any]:
    return {
        "q": args.query,
        "iCalUID": args.i_cal_uid,
        "timeMin": _rfc3339(args.time_min),
        "timeMax": _rfc3339(args.time_max),
        "timeZone": args.time_zone,
        "maxResults": args.max_results,
        "pageToken": args.page_token,
        "syncToken": args.sync_token,
        "singleEvents": args.single_events,
        "showDeleted": args.show_deleted,
        "orderBy": args.order_by,
    }


def _acl_body(args: GoogleCalendarInput, *, include_scope: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"role": args.role}
    if include_scope:
        body["scope"] = _without_none({"type": args.scope_type, "value": args.scope_value})
    return body


def _calendar_list_body(args: GoogleCalendarInput) -> dict[str, Any]:
    return _without_none(
        {
            "summaryOverride": args.summary_override,
            "selected": args.selected,
            "hidden": args.hidden,
            "colorId": args.color_id,
            "backgroundColor": args.background_color,
            "foregroundColor": args.foreground_color,
            "defaultReminders": (
                [{"method": "popup", "minutes": minutes} for minutes in args.reminder_minutes]
                if args.reminder_minutes is not None
                else None
            ),
        }
    )


def _event_time(value: datetime | date | None, time_zone: str | None):
    if value is None:
        return None
    if isinstance(value, datetime):
        result = {"dateTime": value.isoformat()}
        if time_zone:
            result["timeZone"] = time_zone
        return result
    return {"date": value.isoformat()}


def _rfc3339(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _resources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items",):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    calendars = payload.get("calendars")
    if isinstance(calendars, dict):
        return [
            {"id": calendar_id, **value}
            for calendar_id, value in calendars.items()
            if isinstance(value, dict)
        ]
    return [payload] if payload else []


def _summary(operation: str, count: int) -> str:
    if operation in {"list_events", "list_instances"}:
        if count:
            return f"I found {count} calendar event(s)."
        return "I couldn't find any calendar events."
    if count:
        return f"Google Calendar {operation} completed with {count} result(s)."
    return f"Google Calendar {operation} completed."


def _display(args: GoogleCalendarInput, resources: list[dict[str, Any]]) -> str:
    operation = args.operation
    if not resources:
        if operation == "list_events":
            if args.query:
                return "I couldn't find any calendar events matching your search."
            if args.time_min and args.time_max:
                return "You don't have any calendar events in that time range."
            return "You don't have any upcoming events on your calendar."
        if operation == "list_instances":
            return "There are no occurrences of that calendar event in the requested range."
        if operation == "list_calendars":
            return "You don't have any Google Calendars available."
        return f"Google Calendar {operation} completed."
    if operation == "list_events":
        heading = "Calendar events:"
        if args.query:
            heading = "Matching calendar events:"
        elif args.time_min and not args.time_max:
            heading = "Upcoming calendar events:"
    elif operation == "list_instances":
        heading = "Calendar event occurrences:"
    else:
        heading = None
    lines = []
    for item in resources[:50]:
        title = str(item.get("summary") or item.get("id") or "Calendar item")[:500]
        detail = _item_description(item)
        lines.append(f"- {title}" + (f"\n  {detail}" if detail else ""))
    return "\n".join(([heading] if heading else []) + lines)


def _item_description(item: dict[str, Any]) -> str | None:
    start = item.get("start")
    if isinstance(start, dict):
        when = start.get("dateTime") or start.get("date")
        location = item.get("location")
        return " • ".join(str(value)[:1000] for value in (when, location) if value) or None
    description = item.get("description")
    return str(description)[:2000] if description else None


def _short_id(item: dict[str, Any]) -> str:
    raw = str(item.get("id") or "item").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:10]


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
