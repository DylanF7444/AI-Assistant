from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from assistant.contracts import PermissionContext, SkillCall
from assistant.scheduling.contracts import (
    AutonomyPolicy,
    OneTimeTrigger,
    RecurrenceTrigger,
)
from assistant.scheduling.recurrence import next_occurrence, occurrences


def test_background_permission_context_is_bound_to_schedule_revision():
    schedule_id = uuid4()
    context = PermissionContext(
        session_id=f"scheduler:{schedule_id}",
        invocation="background",
        schedule_id=schedule_id,
        schedule_revision=2,
    )
    call = SkillCall(
        skill_id="core.datetime",
        caller="scheduler",
        arguments={"query": "time"},
        permission_context=context,
    )

    assert call.permission_context.schedule_id == schedule_id
    assert call.permission_context.schedule_revision == 2


def test_background_and_interactive_context_cannot_be_confused():
    with pytest.raises(ValidationError):
        PermissionContext(session_id="bad", invocation="background")
    with pytest.raises(ValidationError):
        PermissionContext(session_id="bad", schedule_id=uuid4(), schedule_revision=1)
    with pytest.raises(ValidationError):
        SkillCall(
            skill_id="core.datetime",
            caller="scheduler",
            permission_context=PermissionContext(session_id="interactive"),
        )


def test_scheduling_contracts_reject_naive_times_and_invalid_recurrence():
    with pytest.raises(ValidationError):
        OneTimeTrigger(run_at=datetime(2026, 7, 14, 12, 0))
    with pytest.raises(ValidationError):
        RecurrenceTrigger(
            frequency="weekly",
            start_at=datetime(2026, 7, 14, tzinfo=UTC),
            timezone="America/Chicago",
            local_time=time(9),
        )
    with pytest.raises(ValidationError):
        AutonomyPolicy(allow_planning=False, max_replans=1)


def test_one_time_and_interval_occurrences_are_strictly_after_cursor():
    start = datetime(2026, 7, 14, 12, tzinfo=UTC)
    one_time = OneTimeTrigger(run_at=start)
    assert next_occurrence(one_time, start.replace(hour=11)) == start
    assert next_occurrence(one_time, start) is None

    interval = RecurrenceTrigger(
        frequency="minutes",
        interval=15,
        start_at=start,
        timezone="America/Chicago",
    )
    assert occurrences(interval, start, limit=3) == (
        datetime(2026, 7, 14, 12, 15, tzinfo=UTC),
        datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        datetime(2026, 7, 14, 12, 45, tzinfo=UTC),
    )


def test_daily_recurrence_keeps_local_wall_time_across_dst_start():
    chicago = ZoneInfo("America/Chicago")
    trigger = RecurrenceTrigger(
        frequency="daily",
        start_at=datetime(2026, 3, 7, 9, 0, tzinfo=chicago),
        timezone="America/Chicago",
        local_time=time(9),
    )

    values = occurrences(trigger, datetime(2026, 3, 7, 8, 0, tzinfo=chicago), limit=3)

    assert [value.astimezone(chicago).hour for value in values] == [9, 9, 9]
    assert values[0] == datetime(2026, 3, 7, 15, 0, tzinfo=UTC)
    assert values[1] == datetime(2026, 3, 8, 14, 0, tzinfo=UTC)


def test_nonexistent_and_ambiguous_local_time_policies_are_explicit():
    chicago = ZoneInfo("America/Chicago")
    shifted = RecurrenceTrigger(
        frequency="daily",
        start_at=datetime(2026, 3, 7, 2, 30, tzinfo=chicago),
        timezone="America/Chicago",
        local_time=time(2, 30),
        nonexistent_time="shift_forward",
    )
    skipped = shifted.model_copy(update={"nonexistent_time": "skip"})

    shifted_next = next_occurrence(shifted, datetime(2026, 3, 8, 0, tzinfo=chicago))
    skipped_next = next_occurrence(skipped, datetime(2026, 3, 8, 0, tzinfo=chicago))

    assert shifted_next is not None
    assert shifted_next.astimezone(chicago).time() == time(3)
    assert skipped_next is not None
    assert skipped_next.astimezone(chicago).date().isoformat() == "2026-03-09"

    first = RecurrenceTrigger(
        frequency="daily",
        start_at=datetime(2026, 10, 31, 1, 30, tzinfo=chicago),
        timezone="America/Chicago",
        local_time=time(1, 30),
        ambiguous_time="first",
    )
    second = first.model_copy(update={"ambiguous_time": "second"})
    cursor = datetime(2026, 11, 1, 0, 0, tzinfo=chicago)

    assert next_occurrence(second, cursor) - next_occurrence(first, cursor) == timedelta(hours=1)


def test_monthly_recurrence_skips_months_without_requested_day():
    chicago = ZoneInfo("America/Chicago")
    trigger = RecurrenceTrigger(
        frequency="monthly",
        start_at=datetime(2026, 1, 31, 9, tzinfo=chicago),
        timezone="America/Chicago",
        local_time=time(9),
        day_of_month=31,
    )

    values = occurrences(trigger, datetime(2026, 1, 1, tzinfo=UTC), limit=3)

    assert [value.astimezone(chicago).date().isoformat() for value in values] == [
        "2026-01-31",
        "2026-03-31",
        "2026-05-31",
    ]
