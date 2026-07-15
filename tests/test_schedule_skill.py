from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from assistant.contracts import PermissionContext, ResultStatus, SkillCall
from assistant.scheduling.notifications import NotificationCenter
from assistant.scheduling.runner import BackgroundRunner
from assistant.scheduling.service import SchedulerService
from assistant.scheduling.store import ScheduleStore
from assistant.skills.scheduling import ScheduleSkill
from assistant.storage.database import AuditStore, Database


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def call(arguments: dict) -> SkillCall:
    return SkillCall(
        skill_id="core.schedule",
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


@pytest.fixture
def scheduling(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'skill.db'}")
    database.migrate()
    clock = FakeClock(datetime(2026, 7, 14, 12, tzinfo=UTC))
    store = ScheduleStore(database, clock)
    skill = ScheduleSkill(store, "America/Chicago")
    notifications = NotificationCenter()
    scheduler = SchedulerService(
        store,
        BackgroundRunner(store, notifications, AuditStore(database), clock),
        clock=clock,
    )
    return skill, store, scheduler, notifications, clock


@pytest.mark.asyncio
async def test_create_list_pause_resume_update_and_delete_recurring_schedule(scheduling):
    skill, store, _, _, _ = scheduling
    created = await skill.execute(
        call(
            {
                "operation": "create",
                "name": "Review tasks",
                "message": "Review tasks",
                "recurrence": {
                    "frequency": "weekly",
                    "local_time": "08:30:00",
                    "weekdays": [0, 1, 2, 3, 4],
                },
            }
        ),
        asyncio.Event(),
    )
    schedule_id = created.data["schedules"][0]["schedule_id"]

    listed = await skill.execute(call({"operation": "list"}), asyncio.Event())
    paused = await skill.execute(
        call({"operation": "pause", "schedule": "Review tasks"}), asyncio.Event()
    )
    resumed = await skill.execute(
        call({"operation": "resume", "schedule": schedule_id}), asyncio.Event()
    )
    updated = await skill.execute(
        call(
            {
                "operation": "update",
                "schedule": schedule_id,
                "message": "Review the most important tasks",
            }
        ),
        asyncio.Event(),
    )
    deleted = await skill.execute(
        call({"operation": "delete", "schedule": schedule_id}), asyncio.Event()
    )

    assert created.status == ResultStatus.COMPLETED
    assert listed.data["schedules"][0]["name"] == "Review tasks"
    assert paused.data["schedules"][0]["status"] == "paused"
    assert resumed.data["schedules"][0]["status"] == "active"
    assert updated.data["schedules"][0]["revision"] == 2
    assert updated.data["schedules"][0]["action"]["message"] == ("Review the most important tasks")
    assert deleted.data["schedules"][0]["status"] == "deleted"
    assert store.get_authorization(UUID(schedule_id)) is None


@pytest.mark.asyncio
async def test_run_now_and_history_use_durable_scheduler_path(scheduling):
    skill, _, scheduler, notifications, _ = scheduling
    created = await skill.execute(
        call(
            {
                "operation": "create",
                "name": "Hydrate",
                "message": "Drink water",
                "recurrence": {"frequency": "hours", "interval": 2},
            }
        ),
        asyncio.Event(),
    )
    schedule_id = created.data["schedules"][0]["schedule_id"]

    queued = await skill.execute(
        call({"operation": "run_now", "schedule": schedule_id}), asyncio.Event()
    )
    assert queued.data["runs"][0]["state"] == "pending"

    assert await scheduler.run_once() == 1
    history = await skill.execute(
        call({"operation": "history", "schedule": schedule_id}), asyncio.Event()
    )

    assert notifications.history()[0].message == "Drink water"
    assert history.data["runs"][0]["state"] == "succeeded"


@pytest.mark.asyncio
async def test_snooze_creates_a_new_authorized_one_time_schedule(scheduling):
    skill, store, _, _, clock = scheduling
    created = await skill.execute(
        call(
            {
                "operation": "create",
                "name": "Stretch",
                "message": "Stretch",
                "recurrence": {"frequency": "hours", "interval": 1},
            }
        ),
        asyncio.Event(),
    )
    schedule_id = created.data["schedules"][0]["schedule_id"]

    snoozed = await skill.execute(
        call(
            {
                "operation": "snooze",
                "schedule": schedule_id,
                "delay_seconds": 600,
            }
        ),
        asyncio.Event(),
    )

    assert (
        snoozed.data["schedules"][0]["next_run_at"]
        == (clock.now() + timedelta(minutes=10)).isoformat()
    )
    assert len(store.list()) == 2
