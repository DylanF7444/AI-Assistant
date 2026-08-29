from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from assistant.calendar.contracts import (
    CalendarEvent,
    CalendarLayout,
    CalendarProviderDefinition,
    CalendarProviderStatus,
    CalendarQuery,
    CalendarState,
    CalendarView,
)
from assistant.calendar.google import GoogleCalendarProvider
from assistant.calendar.registry import CalendarProviderRegistry
from assistant.calendar.service import CalendarService
from assistant.calendar.storage import CalendarLayoutStore
from assistant.contracts import PermissionScope
from assistant.integrations.connections import ConnectionStatus
from assistant.integrations.google_calendar import CALENDAR_SCOPE
from assistant.policy.permissions import GrantType, PermissionManager
from assistant.storage.database import AuditStore, Database, SettingsStore


def _query(
    provider_id: str = "test",
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    **updates,
) -> CalendarQuery:
    start = start or datetime(2026, 8, 1, tzinfo=UTC)
    end = end or start + timedelta(days=31)
    return CalendarQuery(
        provider_id=provider_id,
        calendar_ids=("primary",),
        start=start,
        end=end,
        **updates,
    )


def _event(event_id: str, start: datetime, *, title: str = "Event") -> CalendarEvent:
    return CalendarEvent(
        event_id=event_id,
        calendar_id="primary",
        title=title,
        start_at=start,
        end_at=start + timedelta(hours=1),
    )


class _Provider:
    definition = CalendarProviderDefinition(
        provider_id="test",
        display_name="Test calendar",
        description="A deterministic provider for tests.",
        source_label="Test Calendar",
        remote=False,
        requires_connection=False,
    )

    def __init__(self, events: tuple[CalendarEvent, ...] = ()) -> None:
        self.events = events
        self.fail = False

    def validate_query(self, query: CalendarQuery) -> None:
        if query.provider_id != self.definition.provider_id:
            raise ValueError("wrong provider")

    def required_capabilities(self, _query: CalendarQuery) -> tuple[PermissionScope, ...]:
        return ()

    def permission_subject_for(self, _query: CalendarQuery) -> str:
        return "dashboard.test"

    def status(self, _query: CalendarQuery) -> CalendarProviderStatus:
        return CalendarProviderStatus(
            provider_id="test",
            state=CalendarState.READY,
            message="ready",
        )

    async def collect(self, _query: CalendarQuery) -> tuple[CalendarEvent, ...]:
        if self.fail:
            raise RuntimeError("provider failed")
        return self.events


def _service(tmp_path, provider, *, permissions=None) -> CalendarService:
    database = Database(f"sqlite:///{tmp_path / 'calendar.db'}")
    database.migrate()
    registry = CalendarProviderRegistry()
    registry.register(provider)
    return CalendarService(
        registry,
        SettingsStore(database),
        permissions=permissions,
        offline_lock=False,
    )


def test_calendar_contracts_bound_event_shape_and_layout_preferences():
    layout = CalendarLayout(
        view=CalendarView.WEEK,
        provider_id="test",
        calendar_ids=(" primary ", "primary", "work"),
    )
    assert layout.provider_id == "test"
    assert layout.calendar_ids == ("primary", "work")

    all_day = CalendarEvent(
        event_id="holiday",
        calendar_id="primary",
        start_date=date(2026, 8, 3),
        end_date=date(2026, 8, 4),
        all_day=True,
    )
    assert all_day.sort_at == datetime(2026, 8, 3, tzinfo=UTC)

    with pytest.raises(ValidationError):
        CalendarEvent(
            event_id="invalid",
            calendar_id="primary",
            title="bad\nvalue",
            start_at=datetime(2026, 8, 3, tzinfo=UTC),
            end_at=datetime(2026, 8, 3, 1, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        CalendarQuery(
            provider_id="test",
            start=datetime(2026, 8, 1),
            end=datetime(2026, 8, 2, tzinfo=UTC),
        )


def test_calendar_layout_store_recovers_from_invalid_payload(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'layout.db'}")
    database.migrate()
    settings = SettingsStore(database)
    store = CalendarLayoutStore(settings)
    assert store.load() == CalendarLayout()

    store.save(CalendarLayout(view=CalendarView.AGENDA, horizon_days=14))
    assert store.load().view == CalendarView.AGENDA
    settings.set(store.KEY, {"view": "not-a-view"})
    assert store.load() == CalendarLayout()


@pytest.mark.asyncio
async def test_calendar_service_refreshes_sorts_and_keeps_last_good_snapshot(tmp_path):
    first = _event("later", datetime(2026, 8, 2, 10, tzinfo=UTC))
    second = _event("earlier", datetime(2026, 8, 1, 10, tzinfo=UTC))
    provider = _Provider((first, second))
    service = _service(tmp_path, provider)

    initial = await service.refresh(_query())
    assert initial.state == CalendarState.READY
    assert tuple(event.event_id for event in initial.events) == ("earlier", "later")

    provider.fail = True
    stale = await service.refresh(_query())
    assert stale.state == CalendarState.STALE
    assert tuple(event.event_id for event in stale.events) == ("earlier", "later")
    assert stale.error_code == "runtimeerror"
    diagnostics = service.diagnostics()
    assert diagnostics["refresh_started"] == 2
    assert diagnostics[CalendarState.READY.value] == 1
    assert diagnostics[CalendarState.STALE.value] == 1


@pytest.mark.asyncio
async def test_calendar_service_requires_and_records_scoped_dashboard_permission(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'permissions.db'}")
    database.migrate()
    permissions = PermissionManager(database, AuditStore(database))

    class ProtectedProvider(_Provider):
        def required_capabilities(self, _query):
            return (
                PermissionScope(
                    capability="integration.read",
                    targets=("google-calendar:primary:*",),
                    reason="Read calendar events.",
                ),
            )

    provider = ProtectedProvider((_event("one", datetime(2026, 8, 1, tzinfo=UTC)),))
    service = _service(tmp_path, provider, permissions=permissions)
    query = _query()

    denied = await service.refresh(query)
    assert denied.state == CalendarState.PERMISSION_REQUIRED
    request = service.permission_request(CalendarLayout(provider_id="test"))
    assert request.subject_id == "dashboard.test"
    service.grant_permissions(GrantType.SESSION, CalendarLayout(provider_id="test"))

    allowed = await service.refresh(query)
    assert allowed.state == CalendarState.READY
    assert permissions.list_grants()[0]["skill_id"] == "dashboard.test"


class _GoogleAdapter:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests: list[dict] = []

    async def request(self, method, path, *, query=None, body=None):
        self.requests.append({"method": method, "path": path, "query": query, "body": body})
        return self.pages.pop(0)


def _google_query(**updates) -> CalendarQuery:
    return _query("google", **updates)


@pytest.mark.asyncio
async def test_google_calendar_provider_maps_events_filters_and_pages():
    adapter = _GoogleAdapter(
        [
            {
                "items": [
                    {
                        "id": "timed",
                        "summary": "Planning",
                        "start": {"dateTime": "2026-08-01T10:00:00-05:00"},
                        "end": {"dateTime": "2026-08-01T11:00:00-05:00"},
                        "htmlLink": "https://calendar.google.com/event?eid=timed",
                    },
                    {
                        "id": "all-day",
                        "summary": "Holiday",
                        "start": {"date": "2026-08-02"},
                        "end": {"date": "2026-08-03"},
                    },
                    {
                        "id": "declined",
                        "summary": "Declined",
                        "start": {"dateTime": "2026-08-03T10:00:00Z"},
                        "end": {"dateTime": "2026-08-03T11:00:00Z"},
                        "attendees": [{"self": True, "responseStatus": "declined"}],
                    },
                ],
                "nextPageToken": "page-2",
            },
            {
                "items": [
                    {
                        "id": "cancelled",
                        "status": "cancelled",
                        "summary": "Cancelled",
                        "start": {"dateTime": "2026-08-04T10:00:00Z"},
                        "end": {"dateTime": "2026-08-04T11:00:00Z"},
                    }
                ]
            },
        ]
    )
    connection = ConnectionStatus(
        connected=True,
        account="person@example.com",
        granted_scopes=(CALENDAR_SCOPE,),
    )
    provider = GoogleCalendarProvider(
        object(),
        connection_service=type(
            "Connection", (), {"status": lambda _self, **_kwargs: connection}
        )(),
        adapter=adapter,
    )

    events = await provider.collect(_google_query(show_declined=False))
    assert tuple(event.event_id for event in events) == ("timed", "all-day")
    assert events[0].html_link == "https://calendar.google.com/event?eid=timed"
    assert adapter.requests[0]["query"]["showDeleted"] is False
    assert adapter.requests[0]["query"]["pageToken"] is None
    assert adapter.requests[1]["query"]["pageToken"] == "page-2"

    adapter.pages = [
        {
            "items": [
                {
                    "id": "cancelled",
                    "status": "cancelled",
                    "summary": "Cancelled",
                    "start": {"dateTime": "2026-08-04T10:00:00Z"},
                    "end": {"dateTime": "2026-08-04T11:00:00Z"},
                }
            ]
        }
    ]
    cancelled = await provider.collect(_google_query(include_cancelled=True, show_declined=True))
    assert cancelled[-1].event_id == "cancelled"
    assert provider.status(_google_query()).state == CalendarState.READY


def test_google_calendar_provider_reflects_extension_and_connection_state():
    provider = GoogleCalendarProvider(
        object(),
        extension_installed=lambda: False,
        connection_service=type(
            "Connection", (), {"status": lambda _self, **_kwargs: ConnectionStatus(False)}
        )(),
        adapter=_GoogleAdapter([]),
    )
    assert provider.status(_google_query()).state == CalendarState.UNAVAILABLE
