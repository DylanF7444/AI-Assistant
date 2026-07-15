from __future__ import annotations

import calendar
import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from assistant.scheduling.contracts import (
    OneTimeTrigger,
    RecurrenceTrigger,
    ScheduledTrigger,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Scheduling datetimes must include a time zone")
    return value.astimezone(UTC)


def _valid_local_candidates(naive: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    values: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive and all(
            existing.utcoffset() != candidate.utcoffset() for existing in values
        ):
            values.append(candidate)
    return tuple(values)


def _resolve_local(
    local_date: date,
    trigger: RecurrenceTrigger,
) -> datetime | None:
    assert trigger.local_time is not None
    zone = ZoneInfo(trigger.timezone)
    naive = datetime.combine(local_date, trigger.local_time)
    candidates = _valid_local_candidates(naive, zone)
    if candidates:
        index = 1 if len(candidates) == 2 and trigger.ambiguous_time == "second" else 0
        return candidates[index].astimezone(UTC)
    if trigger.nonexistent_time == "skip":
        return None
    for minutes in range(1, 181):
        shifted = naive + timedelta(minutes=minutes)
        candidates = _valid_local_candidates(shifted, zone)
        if candidates:
            return candidates[0].astimezone(UTC)
    raise ValueError("Could not resolve local time within a daylight-saving transition")


def _interval_occurrence(trigger: RecurrenceTrigger, after: datetime) -> datetime | None:
    step = timedelta(
        minutes=trigger.interval if trigger.frequency == "minutes" else 0,
        hours=trigger.interval if trigger.frequency == "hours" else 0,
    )
    start = _utc(trigger.start_at)
    after_utc = _utc(after)
    if after_utc < start:
        candidate = start
    else:
        periods = math.floor((after_utc - start) / step) + 1
        candidate = start + step * periods
    if trigger.end_at is not None and candidate > _utc(trigger.end_at):
        return None
    return candidate


def _month_offset(start: date, candidate: date) -> int:
    return (candidate.year - start.year) * 12 + candidate.month - start.month


def _next_calendar_occurrence(
    trigger: RecurrenceTrigger,
    after: datetime,
) -> datetime | None:
    zone = ZoneInfo(trigger.timezone)
    after_utc = _utc(after)
    start_utc = _utc(trigger.start_at)
    start_local = trigger.start_at.astimezone(zone).date()
    cursor = max(after_utc.astimezone(zone).date(), start_local)

    for offset in range(0, 366 * 20):
        candidate_date = cursor + timedelta(days=offset)
        matches = False
        if trigger.frequency == "daily":
            matches = (candidate_date - start_local).days % trigger.interval == 0
        elif trigger.frequency == "weekly":
            weeks = (candidate_date - start_local).days // 7
            matches = (
                weeks >= 0
                and weeks % trigger.interval == 0
                and (candidate_date.weekday() in trigger.weekdays)
            )
        elif trigger.frequency == "monthly":
            months = _month_offset(start_local, candidate_date)
            matches = (
                months >= 0
                and months % trigger.interval == 0
                and candidate_date.day == trigger.day_of_month
                and trigger.day_of_month
                <= calendar.monthrange(candidate_date.year, candidate_date.month)[1]
            )
        if not matches:
            continue
        candidate = _resolve_local(candidate_date, trigger)
        if candidate is None or candidate < start_utc or candidate <= after_utc:
            continue
        if trigger.end_at is not None and candidate > _utc(trigger.end_at):
            return None
        return candidate
    return None


def next_occurrence(trigger: ScheduledTrigger, after: datetime) -> datetime | None:
    after_utc = _utc(after)
    if isinstance(trigger, OneTimeTrigger):
        run_at = _utc(trigger.run_at)
        return run_at if run_at > after_utc else None
    if trigger.frequency in {"minutes", "hours"}:
        return _interval_occurrence(trigger, after_utc)
    return _next_calendar_occurrence(trigger, after_utc)


def occurrences(
    trigger: ScheduledTrigger,
    after: datetime,
    *,
    limit: int = 3,
) -> tuple[datetime, ...]:
    if limit < 0 or limit > 100:
        raise ValueError("limit must be between 0 and 100")
    values: list[datetime] = []
    cursor = after
    for _ in range(limit):
        value = next_occurrence(trigger, cursor)
        if value is None:
            break
        values.append(value)
        cursor = value
    return tuple(values)
