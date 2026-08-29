"""Provider-neutral calendar contracts and read services."""

from assistant.calendar.contracts import (
    CalendarEvent,
    CalendarLayout,
    CalendarPermissionRequest,
    CalendarProviderDefinition,
    CalendarProviderStatus,
    CalendarQuery,
    CalendarSnapshot,
    CalendarState,
    CalendarView,
)
from assistant.calendar.google import GoogleCalendarProvider
from assistant.calendar.registry import CalendarProvider, CalendarProviderRegistry
from assistant.calendar.service import CalendarService
from assistant.calendar.storage import CalendarLayoutStore

__all__ = [
    "CalendarEvent",
    "CalendarLayout",
    "CalendarPermissionRequest",
    "CalendarProviderDefinition",
    "CalendarProvider",
    "CalendarProviderRegistry",
    "CalendarProviderStatus",
    "CalendarQuery",
    "CalendarService",
    "CalendarSnapshot",
    "CalendarState",
    "CalendarLayoutStore",
    "CalendarView",
    "GoogleCalendarProvider",
]
