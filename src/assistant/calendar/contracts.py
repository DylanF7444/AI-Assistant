from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from assistant.contracts import PermissionScope, StrictModel


class CalendarView(StrEnum):
    """The presentation modes supported by the reusable calendar surface."""

    MONTH = "month"
    WEEK = "week"
    AGENDA = "agenda"


class CalendarState(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    STALE = "stale"
    PERMISSION_REQUIRED = "permission_required"
    DISCONNECTED = "disconnected"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    UNAVAILABLE = "unavailable"
    OFFLINE = "offline"
    ERROR = "error"


class CalendarLayout(StrictModel):
    """Persisted, provider-neutral calendar surface preferences.

    The selected date is deliberately not persisted. A calendar should reopen at
    the current date after a restart, while view, source, and display preferences
    remain stable. Providers may add their own configuration without changing the
    layout contract by using their provider-specific source settings elsewhere.
    """

    layout_version: Literal[1] = 1
    view: CalendarView = CalendarView.MONTH
    provider_id: str = Field(default="google", pattern=r"^[a-z][a-z0-9_.-]+$")
    calendar_ids: tuple[str, ...] = ("primary",)
    first_day_of_week: int = Field(default=0, ge=0, le=6)
    show_weekends: bool = True
    show_declined: bool = True
    horizon_days: int = Field(default=31, ge=1, le=90)
    max_events: int = Field(default=100, ge=1, le=250)

    @field_validator("calendar_ids", mode="before")
    @classmethod
    def _normalize_calendar_ids(cls, value):
        if not isinstance(value, (list, tuple)):
            return value
        normalized = tuple(
            dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
        )
        if not normalized:
            raise ValueError("At least one calendar ID is required")
        if len(normalized) > 20:
            raise ValueError("A calendar layout may select at most 20 calendars")
        return normalized


class CalendarQuery(StrictModel):
    """A bounded read request supplied to a calendar provider."""

    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    calendar_ids: tuple[str, ...] = ("primary",)
    start: datetime
    end: datetime
    max_events: int = Field(default=100, ge=1, le=250)
    include_cancelled: bool = False
    show_declined: bool = True
    time_zone: str | None = Field(default=None, max_length=100)

    @field_validator("calendar_ids", mode="before")
    @classmethod
    def _normalize_query_calendar_ids(cls, value):
        if not isinstance(value, (list, tuple)):
            return value
        normalized = tuple(
            dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
        )
        if not normalized:
            raise ValueError("At least one calendar ID is required")
        if len(normalized) > 20:
            raise ValueError("A calendar query may select at most 20 calendars")
        return normalized

    @model_validator(mode="after")
    def _validate_range(self) -> CalendarQuery:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Calendar query bounds must include a timezone")
        if self.start >= self.end:
            raise ValueError("Calendar query start must be earlier than end")
        return self


class CalendarEvent(StrictModel):
    """A safe display model shared by Google and future calendar providers."""

    event_id: str = Field(min_length=1, max_length=1024)
    calendar_id: str = Field(min_length=1, max_length=1024)
    title: str = Field(default="(No title)", min_length=1, max_length=500)
    start_at: datetime | None = None
    end_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    all_day: bool = False
    time_zone: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=1_000)
    status: Literal["confirmed", "tentative", "cancelled"] = "confirmed"
    transparency: Literal["opaque", "transparent"] | None = None
    color: str | None = Field(default=None, max_length=80)
    response_status: Literal["accepted", "declined", "tentative", "needsAction"] | None = None
    recurring_event_id: str | None = Field(default=None, max_length=1024)
    html_link: str | None = Field(default=None, max_length=2_000)
    sensitive: bool = True

    @field_validator(
        "event_id",
        "calendar_id",
        "title",
        "location",
        "time_zone",
        "recurring_event_id",
        "html_link",
    )
    @classmethod
    def _display_text_must_be_safe(cls, value: str | None) -> str | None:
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("Calendar display text cannot contain control characters")
        return value

    @field_validator("html_link")
    @classmethod
    def _link_must_be_https(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("Calendar links must use HTTPS")
        return value

    @model_validator(mode="after")
    def _validate_event_time(self) -> CalendarEvent:
        if self.all_day:
            if self.start_date is None or self.end_date is None:
                raise ValueError("All-day events require start_date and end_date")
            if self.start_at is not None or self.end_at is not None:
                raise ValueError("All-day events cannot include timed bounds")
            if self.start_date >= self.end_date:
                raise ValueError("All-day event end_date must be after start_date")
        else:
            if self.start_at is None or self.end_at is None:
                raise ValueError("Timed events require start_at and end_at")
            if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
                raise ValueError("Timed event bounds must include a timezone")
            if self.start_at >= self.end_at:
                raise ValueError("Event end_at must be after start_at")
            if self.start_date is not None or self.end_date is not None:
                raise ValueError("Timed events cannot include all-day bounds")
        return self

    @property
    def sort_at(self) -> datetime:
        if self.start_at is not None:
            return self.start_at
        assert self.start_date is not None
        return datetime.combine(self.start_date, datetime.min.time(), tzinfo=UTC)


class CalendarProviderDefinition(StrictModel):
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    extension_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    source_label: str = Field(min_length=1, max_length=120)
    minimum_refresh_seconds: int = Field(default=300, ge=15, le=86_400)
    remote: bool = True
    requires_connection: bool = True


class CalendarProviderStatus(StrictModel):
    provider_id: str
    state: CalendarState
    message: str = Field(default="", max_length=500)
    account: str | None = Field(default=None, max_length=320)
    error_code: str | None = Field(default=None, max_length=100)


class CalendarSnapshot(StrictModel):
    provider_id: str
    source_label: str
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    range_start: datetime
    range_end: datetime
    state: CalendarState
    events: tuple[CalendarEvent, ...] = ()
    summary: str = Field(default="", max_length=500)
    account: str | None = Field(default=None, max_length=320)
    error_code: str | None = Field(default=None, max_length=100)
    sync_cursor: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def _validate_range(self) -> CalendarSnapshot:
        if (
            self.collected_at.tzinfo is None
            or self.range_start.tzinfo is None
            or self.range_end.tzinfo is None
        ):
            raise ValueError("Calendar snapshot timestamps must include a timezone")
        if self.range_start >= self.range_end:
            raise ValueError("Calendar snapshot range must be increasing")
        return self


class CalendarPermissionRequest(StrictModel):
    provider_id: str
    display_name: str
    subject_id: str
    calendar_ids: tuple[str, ...]
    scopes: tuple[PermissionScope, ...]
