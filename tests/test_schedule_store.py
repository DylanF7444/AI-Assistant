from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from threading import Barrier
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import inspect

from assistant.scheduling.contracts import (
    AutonomyPolicy,
    DeliveryPolicy,
    NotificationAction,
    RecurrenceTrigger,
    RunPolicy,
    ScheduleDefinition,
    ScheduleRunState,
    ScheduleStatus,
)
from assistant.scheduling.store import ScheduleStore
from assistant.storage.database import (
    BackgroundAuthorizationRow,
    Database,
    ItemRow,
    ScheduleRow,
    ScheduleRunRow,
    SchemaVersionRow,
)


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


@pytest.fixture
def schedule_store(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'schedules.db'}")
    database.migrate()
    clock = FakeClock(datetime(2026, 7, 14, 12, tzinfo=UTC))
    return ScheduleStore(database, clock), clock


def test_notification_schedule_is_durable_authorized_and_materialized_once(schedule_store):
    store, clock = schedule_store
    due = clock.now() + timedelta(minutes=5)
    schedule = store.create_notification(
        name="Check the oven",
        message="Check the oven",
        run_at=due,
    )

    assert schedule.status == ScheduleStatus.ACTIVE
    assert schedule.next_run_at == due
    assert store.get_authorization(schedule.schedule_id) is not None
    assert store.materialize_due() == ()

    clock.advance(timedelta(minutes=5))
    runs = store.materialize_due()

    assert len(runs) == 1
    assert runs[0].state == ScheduleRunState.PENDING
    assert runs[0].scheduled_for == due
    assert store.get(schedule.schedule_id).status == ScheduleStatus.COMPLETED
    assert store.materialize_due() == ()


def test_only_one_runtime_can_claim_an_occurrence(schedule_store):
    store, clock = schedule_store
    store.create_notification(name="Due", message="Due", run_at=clock.now())
    store.materialize_due()
    first_owner = uuid4()
    second_owner = uuid4()

    first = store.claim_next(first_owner)
    second = store.claim_next(second_owner)

    assert first is not None
    assert first.lease_owner == first_owner
    assert first.attempt == 1
    assert second is None


def test_two_store_instances_do_not_duplicate_due_occurrence(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'contended.db'}")
    database.migrate()
    clock = FakeClock(datetime(2026, 7, 14, 12, tzinfo=UTC))
    first_store = ScheduleStore(database, clock)
    second_store = ScheduleStore(database, clock)
    schedule = first_store.create_notification(
        name="Contended",
        message="Contended",
        run_at=clock.now(),
    )
    barrier = Barrier(2)

    def materialize(store):
        barrier.wait()
        return store.materialize_due()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(materialize, (first_store, second_store)))

    assert sum(len(result) for result in results) == 1
    assert len(first_store.list_runs(schedule_id=schedule.schedule_id)) == 1

    claim_barrier = Barrier(2)

    def claim(store):
        claim_barrier.wait()
        return store.claim_next(uuid4())

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(executor.map(claim, (first_store, second_store)))

    assert sum(result is not None for result in claims) == 1


def test_run_transitions_and_conservative_crash_recovery(schedule_store):
    store, clock = schedule_store
    store.create_notification(name="Due", message="Due", run_at=clock.now())
    store.materialize_due()
    owner = uuid4()
    leased = store.claim_next(owner, lease_seconds=5)
    assert leased is not None

    evaluating = store.transition_run(
        leased.run_id,
        expected=(ScheduleRunState.LEASED,),
        state=ScheduleRunState.EVALUATING,
    )
    assert evaluating.state == ScheduleRunState.EVALUATING

    clock.advance(timedelta(seconds=6))
    reconciled = store.reconcile()
    assert reconciled[0].state == ScheduleRunState.PENDING

    leased = store.claim_next(owner, lease_seconds=5)
    assert leased is not None
    running = store.transition_run(
        leased.run_id,
        expected=(ScheduleRunState.LEASED,),
        state=ScheduleRunState.RUNNING,
        correlation_id=uuid4(),
    )
    assert running.started_at == clock.now()

    clock.advance(timedelta(seconds=6))
    unknown = store.reconcile()[0]
    assert unknown.state == ScheduleRunState.UNKNOWN_AFTER_CRASH
    assert "interrupted" in unknown.blocked_reason.lower()
    assert store.claim_next(owner) is None


def test_completed_history_can_be_cleared_without_deleting_pending_runs(schedule_store):
    store, clock = schedule_store
    completed_schedule = store.create_notification(
        name="Completed",
        message="Completed",
        run_at=clock.now(),
    )
    pending_schedule = store.create_notification(
        name="Pending",
        message="Pending",
        run_at=clock.now(),
    )
    store.materialize_due()
    first = store.claim_next(uuid4())
    assert first is not None
    store.transition_run(
        first.run_id,
        expected=(ScheduleRunState.LEASED,),
        state=ScheduleRunState.SUCCEEDED,
    )

    assert store.clear_completed_run_history() == 1
    remaining = store.list_runs()
    assert len(remaining) == 1
    assert remaining[0].state == ScheduleRunState.PENDING
    assert remaining[0].schedule_id in {
        completed_schedule.schedule_id,
        pending_schedule.schedule_id,
    }


def test_recurring_schedule_advances_from_local_rule_without_drift(schedule_store):
    store, clock = schedule_store
    chicago = ZoneInfo("America/Chicago")
    trigger = RecurrenceTrigger(
        frequency="daily",
        local_time=time(9),
        start_at=datetime(2026, 7, 14, 9, tzinfo=chicago),
        timezone="America/Chicago",
    )
    now = clock.now()
    definition = ScheduleDefinition(
        name="Daily status",
        status=ScheduleStatus.PENDING_AUTHORIZATION,
        trigger=trigger,
        action=NotificationAction(title="Status", message="Review status"),
        autonomy_policy=AutonomyPolicy(),
        run_policy=RunPolicy(),
        delivery_policy=DeliveryPolicy(),
        next_run_at=datetime(2026, 7, 14, 14, tzinfo=UTC),
        created_at=now,
        updated_at=now,
    )
    schedule = store.create(definition, authorize=True)

    clock.value = datetime(2026, 7, 14, 14, tzinfo=UTC)
    store.materialize_due()
    advanced = store.get(schedule.schedule_id)

    assert advanced.next_run_at == datetime(2026, 7, 15, 14, tzinfo=UTC)
    assert advanced.status == ScheduleStatus.ACTIVE


def test_catch_up_cap_advances_past_missed_backlog_in_one_materialization(schedule_store):
    store, clock = schedule_store
    trigger = RecurrenceTrigger(
        frequency="minutes",
        interval=1,
        start_at=clock.now() - timedelta(minutes=10),
        timezone="UTC",
    )
    definition = ScheduleDefinition(
        name="Bounded catch up",
        trigger=trigger,
        action=NotificationAction(title="Catch up", message="Catch up"),
        run_policy=RunPolicy(
            misfire="catch_up",
            max_catch_up_runs=3,
            concurrency="queue_one",
        ),
        next_run_at=clock.now() - timedelta(minutes=9),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    schedule = store.create(definition, authorize=True)

    materialized = store.materialize_due()

    assert len(materialized) == 3
    assert sum(run.state == ScheduleRunState.PENDING for run in materialized) == 1
    assert sum(run.state == ScheduleRunState.SKIPPED for run in materialized) == 2
    assert store.get(schedule.schedule_id).next_run_at > clock.now()
    assert store.materialize_due() == ()


def test_thirty_day_daily_schedule_has_no_wall_clock_drift(schedule_store):
    store, clock = schedule_store
    chicago = ZoneInfo("America/Chicago")
    clock.value = datetime(2026, 10, 20, 14, tzinfo=UTC)
    trigger = RecurrenceTrigger(
        frequency="daily",
        local_time=time(9),
        start_at=datetime(2026, 10, 20, 9, tzinfo=chicago),
        timezone="America/Chicago",
    )
    definition = ScheduleDefinition(
        name="Thirty day schedule",
        trigger=trigger,
        action=NotificationAction(title="Daily", message="Daily"),
        next_run_at=clock.now(),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    schedule = store.create(definition, authorize=True)
    owner = uuid4()

    for _ in range(30):
        store.materialize_due()
        run = store.claim_next(owner)
        assert run is not None
        store.transition_run(
            run.run_id,
            expected=(ScheduleRunState.LEASED,),
            state=ScheduleRunState.SUCCEEDED,
        )
        next_run = store.get(schedule.schedule_id).next_run_at
        assert next_run is not None
        clock.value = next_run

    runs = store.list_runs(schedule_id=schedule.schedule_id)
    assert len(runs) == 30
    assert all(run.scheduled_for.astimezone(chicago).hour == 9 for run in runs)


def test_pause_resume_delete_and_authorization_revocation(schedule_store):
    store, clock = schedule_store
    schedule = store.create_notification(
        name="Later",
        message="Later",
        run_at=clock.now() + timedelta(hours=1),
    )

    assert store.pause(schedule.schedule_id).status == ScheduleStatus.PAUSED
    assert store.resume(schedule.schedule_id).status == ScheduleStatus.ACTIVE
    assert store.revoke_authorization(schedule.schedule_id, "Skill changed").status == (
        ScheduleStatus.PENDING_REVIEW
    )
    assert store.get_authorization(schedule.schedule_id) is None
    assert store.delete(schedule.schedule_id).status == ScheduleStatus.DELETED


def test_expired_background_authority_stops_future_materialization(schedule_store):
    store, clock = schedule_store
    definition = ScheduleDefinition(
        name="Expiring",
        trigger=RecurrenceTrigger(
            frequency="hours",
            interval=1,
            start_at=clock.now(),
            timezone="UTC",
        ),
        action=NotificationAction(title="Expiring", message="Expiring"),
        autonomy_policy=AutonomyPolicy(expires_at=clock.now() + timedelta(minutes=5)),
        next_run_at=clock.now() + timedelta(hours=1),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    schedule = store.create(definition, authorize=True)

    clock.advance(timedelta(minutes=5))

    assert store.next_wake_at() == clock.now()
    assert store.materialize_due() == ()
    expired = store.get(schedule.schedule_id)
    assert expired.status == ScheduleStatus.PENDING_REVIEW
    assert expired.next_run_at is None
    assert store.get_authorization(schedule.schedule_id) is None


def test_schema_six_migration_backfills_incomplete_reminders(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    database.migrate()
    with database.session() as session:
        session.add(
            ItemRow(
                kind="reminder",
                title="Legacy reminder",
                body="",
                due_at=datetime(2026, 7, 15, 12, tzinfo=UTC),
                completed=False,
                created_at=datetime(2026, 7, 14, 12, tzinfo=UTC),
            )
        )
        session.get(SchemaVersionRow, 1).version = 6
    BackgroundAuthorizationRow.__table__.drop(database.engine)
    ScheduleRunRow.__table__.drop(database.engine)
    ScheduleRow.__table__.drop(database.engine)

    database.migrate()

    assert inspect(database.engine).has_table("schedules")
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == 12
        reminder = session.query(ItemRow).filter_by(kind="reminder").one()
        assert reminder.completed
    schedules = ScheduleStore(database).list()
    assert len(schedules) == 1
    assert schedules[0].name == "Legacy reminder"
    assert schedules[0].status == ScheduleStatus.ACTIVE


def test_database_backup_preserves_schedules_runs_and_authorizations(tmp_path):
    source = Database(f"sqlite:///{tmp_path / 'source.db'}")
    source.migrate()
    clock = FakeClock(datetime(2026, 7, 14, 12, tzinfo=UTC))
    source_store = ScheduleStore(source, clock)
    schedule = source_store.create_notification(
        name="Backed up reminder",
        message="Backed up reminder",
        run_at=clock.now(),
    )
    source_store.materialize_due()
    backup_path = tmp_path / "scheduler-backup.db"

    source.backup(backup_path)

    restored = Database(f"sqlite:///{backup_path}")
    restored.migrate()
    restored_store = ScheduleStore(restored, clock)
    assert restored_store.get(schedule.schedule_id).name == "Backed up reminder"
    assert restored_store.get_authorization(schedule.schedule_id) is not None
    assert len(restored_store.list_runs(schedule_id=schedule.schedule_id)) == 1


def test_restore_on_different_machine_requires_authorization_review(tmp_path):
    source = Database(f"sqlite:///{tmp_path / 'machine-a.db'}")
    source.migrate()
    clock = FakeClock(datetime(2026, 7, 14, 12, tzinfo=UTC))
    source_store = ScheduleStore(source, clock, machine_identity="machine-a")
    schedule = source_store.create_notification(
        name="Machine-bound reminder",
        message="Machine-bound reminder",
        run_at=clock.now() + timedelta(hours=1),
    )
    backup_path = tmp_path / "moved.db"
    source.backup(backup_path)
    restored = Database(f"sqlite:///{backup_path}")
    restored.migrate()
    restored_store = ScheduleStore(restored, clock, machine_identity="machine-b")

    changed = restored_store.reconcile_authorization_identity()

    assert changed[0].schedule_id == schedule.schedule_id
    assert changed[0].status == ScheduleStatus.PENDING_REVIEW
    assert restored_store.get_authorization(schedule.schedule_id) is None
