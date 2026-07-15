from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

from assistant.bootstrap import build_container
from assistant.scheduling.notifications import NotificationCenter
from assistant.scheduling.runner import BackgroundRunner
from assistant.scheduling.runtime import BackgroundRuntime
from assistant.scheduling.service import SchedulerService
from assistant.scheduling.store import ScheduleStore
from assistant.storage.database import AuditStore, Database


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def scheduling_services(tmp_path, *, clock=None, fallback=30.0):
    database = Database(f"sqlite:///{tmp_path / 'scheduler.db'}")
    database.migrate()
    store = ScheduleStore(database, clock)
    notifications = NotificationCenter()
    audit = AuditStore(database)
    runner = BackgroundRunner(store, notifications, audit)
    scheduler = SchedulerService(
        store,
        runner,
        clock=clock,
        fallback_wake_seconds=fallback,
    )
    return database, store, notifications, scheduler


def test_scheduler_delivers_due_reminder_and_persists_result(tmp_path):
    clock = FakeClock(datetime(2026, 7, 14, 12, tzinfo=UTC))
    database, store, notifications, scheduler = scheduling_services(tmp_path, clock=clock)
    schedule = store.create_notification(
        name="Check the oven",
        message="Check the oven",
        run_at=clock.now(),
    )

    completed = asyncio.run(scheduler.run_once())

    assert completed == 1
    message = notifications.history()[0]
    assert message.message == "Check the oven"
    assert message.schedule_id == schedule.schedule_id
    run = store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert run.state.value == "succeeded"
    assert run.result == {"delivered": True}
    audit = AuditStore(database).query(event_type="schedule_run_completed")
    assert audit[0]["details"]["schedule_id"] == str(schedule.schedule_id)


def test_background_runtime_wakes_for_new_earlier_schedule_and_stops_cleanly(tmp_path):
    _, store, notifications, scheduler = scheduling_services(tmp_path, fallback=1.0)
    delivered = threading.Event()
    notifications.add_listener(lambda _: delivered.set())
    runtime = BackgroundRuntime(scheduler)
    runtime.start()
    try:
        store.create_notification(
            name="Soon",
            message="Soon",
            run_at=datetime.now(UTC) + timedelta(milliseconds=100),
        )
        assert delivered.wait(2.0)
    finally:
        runtime.stop()

    assert not runtime.running


def test_disabled_scheduler_does_not_materialize_or_deliver(tmp_path):
    clock = FakeClock(datetime(2026, 7, 14, 12, tzinfo=UTC))
    _, store, notifications, scheduler = scheduling_services(tmp_path, clock=clock)
    scheduler.enabled = False
    schedule = store.create_notification(name="Disabled", message="No", run_at=clock.now())

    assert asyncio.run(scheduler.run_once()) == 0
    assert notifications.history() == ()
    assert store.list_runs(schedule_id=schedule.schedule_id) == ()


def test_persisted_global_pause_blocks_new_claims(settings):
    container = build_container(settings.model_copy(update={"automations_paused": True}))
    schedule = container.schedule_store.create_notification(
        name="Paused globally",
        message="Paused globally",
        run_at=datetime.now(UTC),
    )

    assert container.scheduler.paused
    assert asyncio.run(container.scheduler.run_once()) == 0
    assert container.schedule_store.list_runs(schedule_id=schedule.schedule_id) == ()

    container.scheduler.resume()
    assert asyncio.run(container.scheduler.run_once()) == 1
