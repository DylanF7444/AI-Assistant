from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError

from assistant.calendar.contracts import (
    CalendarEvent,
    CalendarProviderDefinition,
    CalendarProviderStatus,
    CalendarQuery,
    CalendarState,
)
from assistant.contracts import PermissionScope
from assistant.integrations.connections import ConnectionState, ConnectionStatus
from assistant.integrations.google import GoogleConnectionService, GoogleTokenManager
from assistant.integrations.google_calendar import (
    CALENDAR_SCOPE,
    GoogleCalendarAdapter,
    quote_id,
)
from assistant.storage.secrets import SecretVault


class GoogleCalendarProvider:
    """Read-only dashboard provider backed by the installed Google Calendar skill."""

    definition = CalendarProviderDefinition(
        provider_id="google",
        extension_id="integration.google_calendar",
        display_name="Google Calendar",
        description="Display bounded events from one or more connected Google calendars.",
        source_label="Google Calendar",
        minimum_refresh_seconds=300,
        remote=True,
        requires_connection=True,
    )

    def __init__(
        self,
        vault: SecretVault,
        *,
        connection_service: GoogleConnectionService | Any | None = None,
        adapter: GoogleCalendarAdapter | Any | None = None,
        extension_installed: Callable[[], bool] | None = None,
        extension_availability: Callable[[], str] | None = None,
    ) -> None:
        self.vault = vault
        self.connection_service = connection_service or GoogleConnectionService(vault)
        self.adapter = adapter or GoogleCalendarAdapter(GoogleTokenManager(vault))
        self.extension_installed = extension_installed or (lambda: True)
        self.extension_availability = extension_availability or (lambda: "enabled")

    def validate_query(self, query: CalendarQuery) -> None:
        if query.provider_id != self.definition.provider_id:
            raise ValueError("Google Calendar provider received a different provider ID")

    def required_capabilities(self, query: CalendarQuery) -> tuple[PermissionScope, ...]:
        targets = tuple(f"google-calendar:{calendar_id}:*" for calendar_id in query.calendar_ids)
        return (
            PermissionScope(
                capability="integration.read",
                targets=targets,
                reason="Read approved Google Calendar events for the dashboard.",
            ),
            PermissionScope(
                capability="network",
                targets=("https://www.googleapis.com", "https://oauth2.googleapis.com"),
                reason="Connect to Google Calendar and refresh its OAuth token.",
            ),
        )

    def permission_subject_for(self, _query: CalendarQuery) -> str:
        return "dashboard.google_calendar"

    def status(self, _query: CalendarQuery) -> CalendarProviderStatus:
        if not self.extension_installed():
            return CalendarProviderStatus(
                provider_id=self.definition.provider_id,
                state=CalendarState.UNAVAILABLE,
                message="Install the Google Calendar extension to display events.",
                error_code="extension_not_installed",
            )
        availability = self.extension_availability()
        if availability == "reauthorization_required":
            return CalendarProviderStatus(
                provider_id=self.definition.provider_id,
                state=CalendarState.REAUTHORIZATION_REQUIRED,
                message="Reconnect Google Calendar to restore the required account access.",
                error_code="reauthorization_required",
            )
        if availability in {"disconnected"}:
            return CalendarProviderStatus(
                provider_id=self.definition.provider_id,
                state=CalendarState.DISCONNECTED,
                message="Connect Google Calendar in Extension Hub to display events.",
                error_code="disconnected",
            )
        if availability != "enabled":
            return CalendarProviderStatus(
                provider_id=self.definition.provider_id,
                state=CalendarState.UNAVAILABLE,
                message="Enable the Google Calendar extension to display events.",
                error_code="extension_disabled",
            )

        status = self._connection_status()
        if not status.connected:
            return CalendarProviderStatus(
                provider_id=self.definition.provider_id,
                state=CalendarState.DISCONNECTED,
                message="Connect Google Calendar in Extension Hub to display events.",
                account=status.account,
                error_code="disconnected",
            )
        if status.state in {
            ConnectionState.MISSING_SCOPES,
            ConnectionState.REAUTHORIZATION_REQUIRED,
        }:
            return CalendarProviderStatus(
                provider_id=self.definition.provider_id,
                state=CalendarState.REAUTHORIZATION_REQUIRED,
                message="Reconnect Google Calendar to grant the required calendar scope.",
                account=status.account,
                error_code="missing_scopes",
            )
        if status.state == ConnectionState.ERROR:
            return CalendarProviderStatus(
                provider_id=self.definition.provider_id,
                state=CalendarState.ERROR,
                message="Google Calendar authorization needs attention.",
                account=status.account,
                error_code="connection_error",
            )
        return CalendarProviderStatus(
            provider_id=self.definition.provider_id,
            state=CalendarState.READY,
            message="Google Calendar is connected.",
            account=status.account,
        )

    async def collect(self, query: CalendarQuery) -> tuple[CalendarEvent, ...]:
        events: list[CalendarEvent] = []
        for calendar_id in query.calendar_ids:
            page_token: str | None = None
            seen_page_tokens: set[str] = set()
            page_count = 0
            while len(events) < query.max_events and page_count < 50:
                page_count += 1
                payload = await self.adapter.request(
                    "GET",
                    f"/calendars/{quote_id(calendar_id)}/events",
                    query={
                        "timeMin": query.start.isoformat(),
                        "timeMax": query.end.isoformat(),
                        "timeZone": query.time_zone,
                        "maxResults": min(250, query.max_events - len(events)),
                        "pageToken": page_token,
                        "singleEvents": True,
                        "showDeleted": query.include_cancelled,
                        "orderBy": "startTime",
                    },
                )
                raw_items = payload.get("items")
                if isinstance(raw_items, list):
                    for item in raw_items:
                        if not isinstance(item, dict):
                            continue
                        event = _event_from_resource(item, calendar_id)
                        if event is None:
                            continue
                        if event.status == "cancelled" and not query.include_cancelled:
                            continue
                        if event.response_status == "declined" and not query.show_declined:
                            continue
                        events.append(event)
                        if len(events) >= query.max_events:
                            break
                next_token = payload.get("nextPageToken")
                page_token = (
                    next_token
                    if isinstance(next_token, str)
                    and next_token
                    and len(next_token) <= 4_096
                    and next_token not in seen_page_tokens
                    else None
                )
                if page_token is None:
                    break
                seen_page_tokens.add(page_token)
        return tuple(events[: query.max_events])

    def _connection_status(self) -> ConnectionStatus:
        service = self.connection_service
        try:
            status = service.status(required_scopes=(CALENDAR_SCOPE,))
        except TypeError:
            # Keep compatibility with older provider services that expose only
            # a no-argument status method.
            status = service.status()
        return ConnectionStatus.coerce(
            status,
            provider_id="google",
            required_scopes=(CALENDAR_SCOPE,),
        )


def _event_from_resource(resource: dict[str, Any], calendar_id: str) -> CalendarEvent | None:
    event_id = resource.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None
    start = resource.get("start")
    end = resource.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    title = resource.get("summary")
    title = str(title).strip() if title is not None else ""
    title = title[:500] or "(No title)"
    location = resource.get("location")
    location = str(location)[:1_000] if location else None
    event_status = resource.get("status")
    if event_status not in {"confirmed", "tentative", "cancelled"}:
        event_status = "confirmed"
    response_status = _response_status(resource)
    link = resource.get("htmlLink")
    safe_link = (
        link[:2_000]
        if isinstance(link, str) and link.startswith("https://calendar.google.com/")
        else None
    )
    color = resource.get("colorId")
    color = str(color)[:80] if color is not None else None
    recurring_id = resource.get("recurringEventId")
    recurring_id = str(recurring_id)[:1_024] if recurring_id else None
    if isinstance(start.get("date"), str) and isinstance(end.get("date"), str):
        try:
            start_date = date.fromisoformat(start["date"])
            end_date = date.fromisoformat(end["date"])
        except ValueError:
            return None
        return _build_event(
            event_id=event_id,
            calendar_id=calendar_id,
            title=title,
            start_date=start_date,
            end_date=end_date,
            all_day=True,
            location=location,
            status=event_status,
            transparency=_transparency(resource),
            color=color,
            response_status=response_status,
            recurring_event_id=recurring_id,
            html_link=safe_link,
        )
    start_at = _parse_datetime(start.get("dateTime"))
    end_at = _parse_datetime(end.get("dateTime"))
    if start_at is None or end_at is None:
        return None
    time_zone = start.get("timeZone")
    time_zone = str(time_zone)[:100] if time_zone else None
    return _build_event(
        event_id=event_id,
        calendar_id=calendar_id,
        title=title,
        start_at=start_at,
        end_at=end_at,
        all_day=False,
        time_zone=time_zone,
        location=location,
        status=event_status,
        transparency=_transparency(resource),
        color=color,
        response_status=response_status,
        recurring_event_id=recurring_id,
        html_link=safe_link,
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _build_event(**values: Any) -> CalendarEvent | None:
    try:
        return CalendarEvent(**values)
    except ValidationError:
        # A malformed third-party item should not blank an otherwise useful
        # calendar surface. The provider's bounded parser simply omits it.
        return None


def _response_status(resource: dict[str, Any]) -> str | None:
    attendees = resource.get("attendees")
    if not isinstance(attendees, list):
        return None
    for attendee in attendees:
        if isinstance(attendee, dict) and attendee.get("self") is True:
            value = attendee.get("responseStatus")
            if value in {"accepted", "declined", "tentative", "needsAction"}:
                return value
    return None


def _transparency(resource: dict[str, Any]) -> str | None:
    value = resource.get("transparency")
    return value if value in {"opaque", "transparent"} else None
