from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from pydantic_core import to_jsonable_python
from sqlalchemy import delete as sql_delete
from sqlalchemy import or_, select, update

from assistant.contracts import ResultStatus, RiskLevel
from assistant.scheduling.clock import Clock, SystemClock
from assistant.scheduling.contracts import (
    ACTION_ADAPTER,
    TERMINAL_RUN_STATES,
    TRIGGER_ADAPTER,
    AutonomyPolicy,
    BackgroundAuthorization,
    DeliveryPolicy,
    FixedSkillAction,
    FrozenPlanAction,
    NotificationAction,
    OneTimeTrigger,
    RecurrenceTrigger,
    RunPolicy,
    ScheduleDefinition,
    ScheduleRun,
    ScheduleRunState,
    ScheduleStatus,
    SkillSnapshot,
)
from assistant.scheduling.recurrence import next_occurrence
from assistant.storage.database import (
    AuditStore,
    BackgroundAuthorizationRow,
    Database,
    ScheduleRow,
    ScheduleRunRow,
)
from assistant.windows.identity import current_machine_identity

_SKILL_SNAPSHOT_ADAPTER = TypeAdapter(tuple[SkillSnapshot, ...])


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_jsonable_python(value),
        sort_keys=True,
        separators=(",", ":"),
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def occurrence_key(schedule_id: UUID, revision: int, scheduled_for: datetime) -> str:
    return f"schedule:{schedule_id}:{revision}:{aware_utc(scheduled_for).isoformat()}"


class ScheduleStore:
    def __init__(
        self,
        database: Database,
        clock: Clock | None = None,
        audit: AuditStore | None = None,
        machine_identity: str | None = None,
    ) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.audit = audit
        self.machine_identity = machine_identity or current_machine_identity()
        self._listeners: set[Callable[[], None]] = set()

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.add(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.discard(listener)

    def _notify_changed(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def create(
        self,
        definition: ScheduleDefinition,
        *,
        authorize: bool = False,
    ) -> ScheduleDefinition:
        now = aware_utc(self.clock.now())
        if definition.status == ScheduleStatus.ACTIVE and not authorize:
            raise ValueError("Active schedules require a background authorization")
        next_run = definition.next_run_at or self._initial_occurrence(definition, now)
        status = ScheduleStatus.ACTIVE if authorize else definition.status
        authorized_at = now if authorize else definition.authorized_at
        stored = definition.model_copy(
            update={
                "status": status,
                "next_run_at": next_run,
                "authorized_at": authorized_at,
                "updated_at": now,
            }
        )
        action_payload = canonical_json(stored.action)
        action_digest = digest(stored.action)
        with self.database.session() as session:
            if session.get(ScheduleRow, str(stored.schedule_id)) is not None:
                raise ValueError(f"Schedule already exists: {stored.schedule_id}")
            session.add(self._new_schedule_row(stored, action_payload, action_digest))
            if authorize:
                self._upsert_authorization(session, stored, action_digest, now)
        self._notify_changed()
        created = self.get(stored.schedule_id)
        self._record_schedule_event("schedule_created", created, "created")
        if authorize:
            self._record_schedule_event("schedule_authorized", created, "authorized")
        return created

    def create_notification(
        self,
        *,
        name: str,
        message: str,
        run_at: datetime,
        title: str = "Reminder",
        user_id: str = "local-user",
        description: str = "",
    ) -> ScheduleDefinition:
        now = aware_utc(self.clock.now())
        run_at = aware_utc(run_at)
        definition = ScheduleDefinition(
            user_id=user_id,
            name=name,
            description=description,
            status=ScheduleStatus.PENDING_AUTHORIZATION,
            trigger=OneTimeTrigger(run_at=run_at),
            action=NotificationAction(title=title, message=message),
            next_run_at=run_at,
            created_at=now,
            updated_at=now,
        )
        return self.create(definition, authorize=True)

    def create_recurring_notification(
        self,
        *,
        name: str,
        message: str,
        trigger: RecurrenceTrigger,
        title: str = "Reminder",
        user_id: str = "local-user",
        description: str = "",
        run_policy: RunPolicy | None = None,
    ) -> ScheduleDefinition:
        now = aware_utc(self.clock.now())
        definition = ScheduleDefinition(
            user_id=user_id,
            name=name,
            description=description,
            status=ScheduleStatus.PENDING_AUTHORIZATION,
            trigger=trigger,
            action=NotificationAction(title=title, message=message),
            run_policy=run_policy or RunPolicy(misfire="skip", max_lateness_seconds=3_600),
            created_at=now,
            updated_at=now,
        )
        return self.create(definition, authorize=True)

    def authorize(self, schedule_id: UUID) -> ScheduleDefinition:
        now = aware_utc(self.clock.now())
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            definition = self._definition(row)
            if definition.status in {
                ScheduleStatus.COMPLETED,
                ScheduleStatus.DISABLED,
                ScheduleStatus.DELETED,
            }:
                raise ValueError(f"Cannot authorize a {definition.status.value} schedule")
            row.status = ScheduleStatus.ACTIVE.value
            row.authorized_at = now
            row.updated_at = now
            self._upsert_authorization(session, definition, row.action_digest, now)
        self._notify_changed()
        authorized = self.get(schedule_id)
        self._record_schedule_event("schedule_authorized", authorized, "authorized")
        return authorized

    def get(self, schedule_id: UUID) -> ScheduleDefinition:
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            return self._definition(row)

    def list(
        self,
        *,
        statuses: tuple[ScheduleStatus, ...] | None = None,
    ) -> tuple[ScheduleDefinition, ...]:
        with self.database.session() as session:
            statement = select(ScheduleRow)
            if statuses:
                statement = statement.where(
                    ScheduleRow.status.in_([status.value for status in statuses])
                )
            rows = session.scalars(statement.order_by(ScheduleRow.next_run_at, ScheduleRow.name))
            return tuple(self._definition(row) for row in rows)

    def pause(self, schedule_id: UUID, reason: str = "Paused by user") -> ScheduleDefinition:
        now = aware_utc(self.clock.now())
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            if row.status not in {
                ScheduleStatus.ACTIVE.value,
                ScheduleStatus.PENDING_REVIEW.value,
            }:
                raise ValueError(f"Cannot pause schedule in state {row.status}")
            row.status = ScheduleStatus.PAUSED.value
            row.paused_reason = reason
            row.updated_at = now
        self._notify_changed()
        paused = self.get(schedule_id)
        self._record_schedule_event("schedule_paused", paused, "paused", reason=reason)
        return paused

    def resume(self, schedule_id: UUID) -> ScheduleDefinition:
        now = aware_utc(self.clock.now())
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            if row.status != ScheduleStatus.PAUSED.value:
                raise ValueError(f"Cannot resume schedule in state {row.status}")
            authorization = session.scalar(
                select(BackgroundAuthorizationRow).where(
                    BackgroundAuthorizationRow.schedule_id == row.id,
                    BackgroundAuthorizationRow.schedule_revision == row.revision,
                    BackgroundAuthorizationRow.revoked_at.is_(None),
                )
            )
            if authorization is None:
                row.status = ScheduleStatus.PENDING_AUTHORIZATION.value
            else:
                row.status = ScheduleStatus.ACTIVE.value
            row.paused_reason = None
            row.updated_at = now
        self._notify_changed()
        resumed = self.get(schedule_id)
        self._record_schedule_event("schedule_resumed", resumed, "resumed")
        return resumed

    def delete(self, schedule_id: UUID) -> ScheduleDefinition:
        now = aware_utc(self.clock.now())
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            row.status = ScheduleStatus.DELETED.value
            row.next_run_at = None
            row.updated_at = now
            self._revoke_authorizations(session, row.id, now)
            session.execute(
                update(ScheduleRunRow)
                .where(
                    ScheduleRunRow.schedule_id == row.id,
                    ScheduleRunRow.state.in_(
                        [
                            ScheduleRunState.PENDING.value,
                            ScheduleRunState.AWAITING_APPROVAL.value,
                        ]
                    ),
                )
                .values(
                    state=ScheduleRunState.CANCELLED.value,
                    completed_at=now,
                    blocked_reason="Schedule deleted",
                )
            )
        self._notify_changed()
        deleted = self.get(schedule_id)
        self._record_schedule_event("schedule_deleted", deleted, "deleted")
        return deleted

    def revise(
        self,
        schedule_id: UUID,
        *,
        trigger=None,
        action=None,
        autonomy_policy: AutonomyPolicy | None = None,
        run_policy: RunPolicy | None = None,
        delivery_policy: DeliveryPolicy | None = None,
        authorize: bool = False,
    ) -> ScheduleDefinition:
        now = aware_utc(self.clock.now())
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            current = self._definition(row)
            if current.status in {ScheduleStatus.DELETED, ScheduleStatus.DISABLED}:
                raise ValueError(f"Cannot revise a {current.status.value} schedule")
            revision = current.revision + 1
            revised_action = self._action_for_revision(
                action or current.action,
                current.schedule_id,
                revision,
                current.user_id,
            )
            revised = current.model_copy(
                update={
                    "revision": revision,
                    "trigger": trigger or current.trigger,
                    "action": revised_action,
                    "autonomy_policy": autonomy_policy or current.autonomy_policy,
                    "run_policy": run_policy or current.run_policy,
                    "delivery_policy": delivery_policy or current.delivery_policy,
                    "status": (
                        ScheduleStatus.ACTIVE if authorize else ScheduleStatus.PENDING_AUTHORIZATION
                    ),
                    "authorized_at": now if authorize else None,
                    "updated_at": now,
                    "review_reason": None,
                    "paused_reason": None,
                }
            )
            following = self._initial_occurrence(revised, now)
            revised = revised.model_copy(update={"next_run_at": following})
            action_payload = canonical_json(revised.action)
            action_digest = digest(revised.action)
            self._revoke_authorizations(session, row.id, now)
            session.execute(
                update(ScheduleRunRow)
                .where(
                    ScheduleRunRow.schedule_id == row.id,
                    ScheduleRunRow.state.in_(
                        [
                            ScheduleRunState.PENDING.value,
                            ScheduleRunState.AWAITING_APPROVAL.value,
                        ]
                    ),
                )
                .values(
                    state=ScheduleRunState.CANCELLED.value,
                    completed_at=now,
                    blocked_reason="Schedule was revised",
                )
            )
            replacement = self._new_schedule_row(revised, action_payload, action_digest)
            for column in ScheduleRow.__table__.columns:
                if column.name != "id":
                    setattr(row, column.name, getattr(replacement, column.name))
            if authorize:
                self._upsert_authorization(session, revised, action_digest, now)
        self._notify_changed()
        result = self.get(schedule_id)
        self._record_schedule_event("schedule_updated", result, "revised")
        if authorize:
            self._record_schedule_event("schedule_authorized", result, "authorized")
        return result

    def rename(
        self,
        schedule_id: UUID,
        *,
        name: str,
        description: str | None = None,
    ) -> ScheduleDefinition:
        name = name.strip()
        if not name:
            raise ValueError("Schedule name cannot be empty")
        now = aware_utc(self.clock.now())
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            row.name = name[:200]
            if description is not None:
                row.description = description[:500]
            row.updated_at = now
        self._notify_changed()
        renamed = self.get(schedule_id)
        self._record_schedule_event("schedule_updated", renamed, "renamed")
        return renamed

    def run_now(self, schedule_id: UUID) -> ScheduleRun:
        now = aware_utc(self.clock.now())
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            if row.status != ScheduleStatus.ACTIVE.value:
                raise ValueError(f"Cannot run a schedule in state {row.status}")
            nonce = uuid4()
            key = f"schedule:{row.id}:{row.revision}:manual:{nonce}"
            run_id = str(uuid4())
            session.add(
                ScheduleRunRow(
                    id=run_id,
                    schedule_id=row.id,
                    schedule_revision=row.revision,
                    occurrence_key=key,
                    scheduled_for=now,
                    state=ScheduleRunState.PENDING.value,
                    attempt=0,
                    lateness_seconds=0,
                    idempotency_key=key,
                    action_digest=row.action_digest,
                    created_at=now,
                )
            )
        self._notify_changed()
        run = self.get_run(UUID(run_id))
        self._record_run_event("schedule_occurrence_created", run, "manual")
        return run

    def snooze(self, schedule_id: UUID, delay_seconds: float) -> ScheduleDefinition:
        if delay_seconds <= 0 or delay_seconds > 31_536_000:
            raise ValueError("Snooze delay must be between 0 and one year")
        schedule = self.get(schedule_id)
        if not isinstance(schedule.action, NotificationAction):
            raise ValueError("Only notification schedules can be snoozed")
        return self.create_notification(
            name=f"{schedule.name} (snoozed)",
            message=schedule.action.message,
            title=schedule.action.title,
            run_at=aware_utc(self.clock.now()) + timedelta(seconds=delay_seconds),
            user_id=schedule.user_id,
            description=f"Snoozed from {schedule.schedule_id}",
        )

    def revoke_authorization(self, schedule_id: UUID, reason: str) -> ScheduleDefinition:
        now = aware_utc(self.clock.now())
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            self._revoke_authorizations(session, row.id, now)
            row.status = ScheduleStatus.PENDING_REVIEW.value
            row.review_reason = reason
            row.updated_at = now
        self._notify_changed()
        revoked = self.get(schedule_id)
        self._record_schedule_event(
            "schedule_authorization_revoked",
            revoked,
            "revoked",
            reason=reason,
        )
        return revoked

    def retire_authorization(self, schedule_id: UUID, reason: str) -> ScheduleDefinition:
        """Revoke standing authority without changing a terminal schedule status."""
        now = aware_utc(self.clock.now())
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            self._revoke_authorizations(session, row.id, now)
            row.review_reason = reason
            row.updated_at = now
        schedule = self.get(schedule_id)
        self._record_schedule_event(
            "schedule_authorization_revoked",
            schedule,
            "retired",
            reason=reason,
        )
        self._notify_changed()
        return schedule

    def get_authorization(self, schedule_id: UUID) -> BackgroundAuthorization | None:
        with self.database.session() as session:
            row = session.scalar(
                select(BackgroundAuthorizationRow).where(
                    BackgroundAuthorizationRow.schedule_id == str(schedule_id),
                    BackgroundAuthorizationRow.revoked_at.is_(None),
                )
            )
            return None if row is None else self._authorization(row)

    def materialize_due(self, now: datetime | None = None) -> tuple[ScheduleRun, ...]:
        now = aware_utc(now or self.clock.now())
        created_ids: list[str] = []
        expired_schedule_ids: list[str] = []
        with self.database.session() as session:
            expired_rows = list(
                session.scalars(
                    select(ScheduleRow).where(
                        ScheduleRow.status == ScheduleStatus.ACTIVE.value,
                        ScheduleRow.expires_at.is_not(None),
                        ScheduleRow.expires_at <= now,
                    )
                )
            )
            for expired_row in expired_rows:
                expired_row.status = ScheduleStatus.PENDING_REVIEW.value
                expired_row.review_reason = "Background authorization expired"
                expired_row.next_run_at = None
                expired_row.updated_at = now
                self._revoke_authorizations(session, expired_row.id, now)
                expired_schedule_ids.append(expired_row.id)
            rows = list(
                session.scalars(
                    select(ScheduleRow)
                    .where(
                        ScheduleRow.status == ScheduleStatus.ACTIVE.value,
                        ScheduleRow.next_run_at.is_not(None),
                        ScheduleRow.next_run_at <= now,
                    )
                    .order_by(ScheduleRow.next_run_at, ScheduleRow.id)
                )
            )
            for row in rows:
                definition = self._definition(row)
                assert definition.next_run_at is not None
                scheduled_for = aware_utc(definition.next_run_at)
                if isinstance(definition.trigger, OneTimeTrigger):
                    occurrences = (scheduled_for,)
                    following = None
                elif definition.run_policy.misfire == "catch_up":
                    occurrences_list = []
                    cursor = scheduled_for
                    cap = definition.run_policy.max_catch_up_runs
                    while cursor <= now and len(occurrences_list) < max(1, cap):
                        occurrences_list.append(cursor)
                        next_cursor = next_occurrence(definition.trigger, cursor)
                        if next_cursor is None:
                            cursor = None
                            break
                        cursor = next_cursor
                    occurrences = tuple(occurrences_list)
                    following = (
                        next_occurrence(definition.trigger, now)
                        if cursor is not None and cursor <= now
                        else cursor
                    )
                else:
                    occurrences = (scheduled_for,)
                    following = next_occurrence(
                        definition.trigger,
                        max(scheduled_for, now),
                    )

                for occurrence_index, scheduled_for in enumerate(occurrences):
                    key = occurrence_key(
                        definition.schedule_id,
                        definition.revision,
                        scheduled_for,
                    )
                    existing = session.scalar(
                        select(ScheduleRunRow).where(ScheduleRunRow.occurrence_key == key)
                    )
                    lateness = max(0.0, (now - scheduled_for).total_seconds())
                    skip_reason = None
                    if (
                        definition.run_policy.misfire == "skip"
                        and lateness > definition.run_policy.max_lateness_seconds
                    ):
                        skip_reason = "Occurrence exceeded its maximum lateness"
                    elif (
                        definition.run_policy.misfire == "catch_up"
                        and definition.run_policy.max_catch_up_runs == 0
                    ):
                        skip_reason = "Catch-up is disabled for this schedule"
                    else:
                        blocking_states = (
                            [ScheduleRunState.PENDING.value]
                            if definition.run_policy.concurrency == "queue_one"
                            else [
                                ScheduleRunState.PENDING.value,
                                ScheduleRunState.LEASED.value,
                                ScheduleRunState.EVALUATING.value,
                                ScheduleRunState.AWAITING_APPROVAL.value,
                                ScheduleRunState.RUNNING.value,
                            ]
                        )
                        active_run = session.scalar(
                            select(ScheduleRunRow.id)
                            .where(
                                ScheduleRunRow.schedule_id == row.id,
                                ScheduleRunRow.state.in_(blocking_states),
                            )
                            .limit(1)
                        )
                        if active_run is not None:
                            skip_reason = (
                                "Another occurrence is already queued"
                                if definition.run_policy.concurrency == "queue_one"
                                else "Another occurrence is still active"
                            )
                    if existing is None:
                        run_id = str(uuid4())
                        skipped = skip_reason is not None
                        session.add(
                            ScheduleRunRow(
                                id=run_id,
                                schedule_id=row.id,
                                schedule_revision=row.revision,
                                occurrence_key=key,
                                scheduled_for=scheduled_for,
                                state=(
                                    ScheduleRunState.SKIPPED.value
                                    if skipped
                                    else ScheduleRunState.PENDING.value
                                ),
                                attempt=0,
                                lateness_seconds=lateness,
                                idempotency_key=key,
                                action_digest=row.action_digest,
                                blocked_reason=skip_reason,
                                created_at=now,
                                completed_at=now if skipped else None,
                            )
                        )
                        session.flush()
                        created_ids.append(run_id)
                    row.last_run_at = scheduled_for
                    if occurrence_index == 0:
                        row.updated_at = now

                row.next_run_at = following
                if following is None:
                    row.status = ScheduleStatus.COMPLETED.value
        runs = tuple(self.get_run(UUID(run_id)) for run_id in created_ids)
        for run in runs:
            event_type = (
                "schedule_misfire"
                if run.state == ScheduleRunState.SKIPPED
                else "schedule_occurrence_created"
            )
            self._record_run_event(event_type, run, run.state.value)
        for schedule_id in expired_schedule_ids:
            schedule = self.get(UUID(schedule_id))
            self._record_schedule_event(
                "schedule_authorization_revoked",
                schedule,
                "expired",
            )
        if expired_schedule_ids:
            self._notify_changed()
        return runs

    def claim_next(
        self,
        lease_owner: UUID,
        *,
        now: datetime | None = None,
        lease_seconds: int = 30,
    ) -> ScheduleRun | None:
        now = aware_utc(now or self.clock.now())
        lease_expires = now + timedelta(seconds=lease_seconds)
        with self.database.session() as session:
            candidate_ids = list(
                session.scalars(
                    select(ScheduleRunRow.id)
                    .where(
                        ScheduleRunRow.state == ScheduleRunState.PENDING.value,
                        ScheduleRunRow.scheduled_for <= now,
                        or_(ScheduleRunRow.retry_at.is_(None), ScheduleRunRow.retry_at <= now),
                    )
                    .order_by(ScheduleRunRow.scheduled_for, ScheduleRunRow.created_at)
                    .limit(20)
                )
            )
            claimed_id = None
            for candidate_id in candidate_ids:
                result = session.execute(
                    update(ScheduleRunRow)
                    .where(
                        ScheduleRunRow.id == candidate_id,
                        ScheduleRunRow.state == ScheduleRunState.PENDING.value,
                    )
                    .values(
                        state=ScheduleRunState.LEASED.value,
                        lease_owner=str(lease_owner),
                        lease_expires_at=lease_expires,
                        attempt=ScheduleRunRow.attempt + 1,
                    )
                )
                if result.rowcount == 1:
                    claimed_id = candidate_id
                    break
        if claimed_id is None:
            return None
        claimed = self.get_run(UUID(claimed_id))
        self._record_run_event("schedule_run_claimed", claimed, "claimed")
        return claimed

    def transition_run(
        self,
        run_id: UUID,
        *,
        expected: tuple[ScheduleRunState, ...],
        state: ScheduleRunState,
        correlation_id: UUID | None = None,
        result_status: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        blocked_reason: str | None = None,
        approval_expires_at: datetime | None = None,
        retry_at: datetime | None = None,
        lease_owner: UUID | None = None,
        lease_seconds: int = 30,
    ) -> ScheduleRun:
        now = aware_utc(self.clock.now())
        terminal = state in {
            ScheduleRunState.SUCCEEDED,
            ScheduleRunState.FAILED,
            ScheduleRunState.TIMED_OUT,
            ScheduleRunState.CANCELLED,
            ScheduleRunState.UNKNOWN_AFTER_CRASH,
            ScheduleRunState.REJECTED,
            ScheduleRunState.EXPIRED,
            ScheduleRunState.BLOCKED,
            ScheduleRunState.SKIPPED,
        }
        values: dict[str, Any] = {
            "state": state.value,
            "correlation_id": str(correlation_id) if correlation_id else None,
            "result_status": result_status,
            "result_json": canonical_json(result) if result is not None else None,
            "error_json": canonical_json(error) if error is not None else None,
            "blocked_reason": blocked_reason,
            "approval_expires_at": (
                aware_utc(approval_expires_at) if approval_expires_at else None
            ),
            "retry_at": aware_utc(retry_at) if retry_at else None,
        }
        if state == ScheduleRunState.RUNNING:
            values["started_at"] = now
        if lease_owner is not None:
            values["lease_owner"] = str(lease_owner)
            values["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
        if terminal or state in {
            ScheduleRunState.AWAITING_APPROVAL,
            ScheduleRunState.PENDING,
        }:
            values["lease_owner"] = None
            values["lease_expires_at"] = None
        if terminal:
            values["completed_at"] = now
        with self.database.session() as session:
            updated = session.execute(
                update(ScheduleRunRow)
                .where(
                    ScheduleRunRow.id == str(run_id),
                    ScheduleRunRow.state.in_([item.value for item in expected]),
                )
                .values(**values)
            )
            if updated.rowcount != 1:
                row = session.get(ScheduleRunRow, str(run_id))
                if row is None:
                    raise KeyError(run_id)
                raise ValueError(f"Cannot transition run from {row.state} to {state.value}")
        transitioned = self.get_run(run_id)
        self._record_run_event(
            "schedule_run_transition",
            transitioned,
            transitioned.state.value,
        )
        if transitioned.state == ScheduleRunState.RUNNING:
            self._record_run_event("schedule_run_started", transitioned, "running")
        return transitioned

    def renew_lease(
        self,
        run_id: UUID,
        lease_owner: UUID,
        *,
        lease_seconds: int = 30,
    ) -> bool:
        expires = aware_utc(self.clock.now()) + timedelta(seconds=lease_seconds)
        with self.database.session() as session:
            result = session.execute(
                update(ScheduleRunRow)
                .where(
                    ScheduleRunRow.id == str(run_id),
                    ScheduleRunRow.lease_owner == str(lease_owner),
                    ScheduleRunRow.state.in_(
                        [
                            ScheduleRunState.LEASED.value,
                            ScheduleRunState.EVALUATING.value,
                            ScheduleRunState.RUNNING.value,
                        ]
                    ),
                )
                .values(lease_expires_at=expires)
            )
            return result.rowcount == 1

    def reconcile(self, now: datetime | None = None) -> tuple[ScheduleRun, ...]:
        now = aware_utc(now or self.clock.now())
        changed_ids: list[str] = []
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ScheduleRunRow).where(
                        ScheduleRunRow.state.in_(
                            [
                                ScheduleRunState.LEASED.value,
                                ScheduleRunState.EVALUATING.value,
                                ScheduleRunState.RUNNING.value,
                            ]
                        ),
                        ScheduleRunRow.lease_expires_at.is_not(None),
                        ScheduleRunRow.lease_expires_at <= now,
                    )
                )
            )
            for row in rows:
                if row.state == ScheduleRunState.RUNNING.value:
                    row.state = ScheduleRunState.UNKNOWN_AFTER_CRASH.value
                    row.completed_at = now
                    row.blocked_reason = "Execution was interrupted before its outcome was stored"
                else:
                    row.state = ScheduleRunState.PENDING.value
                row.lease_owner = None
                row.lease_expires_at = None
                changed_ids.append(row.id)
        runs = tuple(self.get_run(UUID(run_id)) for run_id in changed_ids)
        for run in runs:
            self._record_run_event(
                "schedule_run_unknown"
                if run.state == ScheduleRunState.UNKNOWN_AFTER_CRASH
                else "schedule_run_reconciled",
                run,
                run.state.value,
            )
        return runs

    def reconcile_authorization_identity(self) -> tuple[ScheduleDefinition, ...]:
        now = aware_utc(self.clock.now())
        changed_ids: list[str] = []
        with self.database.session() as session:
            authorizations = list(
                session.scalars(
                    select(BackgroundAuthorizationRow).where(
                        BackgroundAuthorizationRow.revoked_at.is_(None),
                        BackgroundAuthorizationRow.machine_identity != self.machine_identity,
                    )
                )
            )
            for authorization in authorizations:
                authorization.revoked_at = now
                row = session.get(ScheduleRow, authorization.schedule_id)
                if row is not None and row.status in {
                    ScheduleStatus.ACTIVE.value,
                    ScheduleStatus.PAUSED.value,
                }:
                    row.status = ScheduleStatus.PENDING_REVIEW.value
                    row.review_reason = "Authorization was restored on another machine"
                    row.updated_at = now
                    changed_ids.append(row.id)
        schedules = tuple(self.get(UUID(schedule_id)) for schedule_id in set(changed_ids))
        for schedule in schedules:
            self._record_schedule_event(
                "schedule_authorization_revoked",
                schedule,
                "machine_identity_changed",
            )
        if schedules:
            self._notify_changed()
        return schedules

    def expire_approvals(self, now: datetime | None = None) -> tuple[ScheduleRun, ...]:
        now = aware_utc(now or self.clock.now())
        expired_ids: list[str] = []
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ScheduleRunRow).where(
                        ScheduleRunRow.state == ScheduleRunState.AWAITING_APPROVAL.value,
                        ScheduleRunRow.approval_expires_at.is_not(None),
                        ScheduleRunRow.approval_expires_at <= now,
                    )
                )
            )
            for row in rows:
                row.state = ScheduleRunState.EXPIRED.value
                row.result_status = ResultStatus.CANCELLED.value
                row.blocked_reason = "Approval window expired"
                row.completed_at = now
                expired_ids.append(row.id)
        runs = tuple(self.get_run(UUID(run_id)) for run_id in expired_ids)
        for run in runs:
            self._record_run_event("schedule_approval_expired", run, "expired")
        return runs

    def get_run(self, run_id: UUID) -> ScheduleRun:
        with self.database.session() as session:
            row = session.get(ScheduleRunRow, str(run_id))
            if row is None:
                raise KeyError(run_id)
            return self._run(row)

    def list_runs(
        self,
        *,
        schedule_id: UUID | None = None,
        states: tuple[ScheduleRunState, ...] | None = None,
        limit: int = 100,
    ) -> tuple[ScheduleRun, ...]:
        with self.database.session() as session:
            statement = select(ScheduleRunRow)
            if schedule_id is not None:
                statement = statement.where(ScheduleRunRow.schedule_id == str(schedule_id))
            if states:
                statement = statement.where(
                    ScheduleRunRow.state.in_([state.value for state in states])
                )
            rows = session.scalars(
                statement.order_by(ScheduleRunRow.created_at.desc()).limit(limit)
            )
            return tuple(self._run(row) for row in rows)

    def clear_completed_run_history(self, schedule_id: UUID | None = None) -> int:
        terminal_states = [state.value for state in TERMINAL_RUN_STATES]
        with self.database.session() as session:
            statement = sql_delete(ScheduleRunRow).where(ScheduleRunRow.state.in_(terminal_states))
            if schedule_id is not None:
                statement = statement.where(ScheduleRunRow.schedule_id == str(schedule_id))
            result = session.execute(statement)
            deleted = int(result.rowcount or 0)
        if self.audit is not None:
            self.audit.record(
                correlation_id=f"schedule-history:{schedule_id or 'all'}",
                event_type="schedule_history_cleared",
                decision="deleted",
                details={
                    "schedule_id": str(schedule_id) if schedule_id else None,
                    "deleted_runs": deleted,
                },
            )
        self._notify_changed()
        return deleted

    def next_wake_at(self) -> datetime | None:
        with self.database.session() as session:
            schedule_due = session.scalar(
                select(ScheduleRow.next_run_at)
                .where(
                    ScheduleRow.status == ScheduleStatus.ACTIVE.value,
                    ScheduleRow.next_run_at.is_not(None),
                )
                .order_by(ScheduleRow.next_run_at)
                .limit(1)
            )
            run_due = session.scalar(
                select(ScheduleRunRow.retry_at)
                .where(
                    ScheduleRunRow.state == ScheduleRunState.PENDING.value,
                    ScheduleRunRow.retry_at.is_not(None),
                )
                .order_by(ScheduleRunRow.retry_at)
                .limit(1)
            )
            approval_due = session.scalar(
                select(ScheduleRunRow.approval_expires_at)
                .where(
                    ScheduleRunRow.state == ScheduleRunState.AWAITING_APPROVAL.value,
                    ScheduleRunRow.approval_expires_at.is_not(None),
                )
                .order_by(ScheduleRunRow.approval_expires_at)
                .limit(1)
            )
            authorization_due = session.scalar(
                select(ScheduleRow.expires_at)
                .where(
                    ScheduleRow.status == ScheduleStatus.ACTIVE.value,
                    ScheduleRow.expires_at.is_not(None),
                )
                .order_by(ScheduleRow.expires_at)
                .limit(1)
            )
        values = [
            aware_utc(value)
            for value in (schedule_due, run_due, approval_due, authorization_due)
            if value is not None
        ]
        return min(values) if values else None

    def count_runs_since(
        self,
        schedule_id: UUID,
        since: datetime,
        *,
        exclude_run_id: UUID | None = None,
    ) -> int:
        with self.database.session() as session:
            statement = select(ScheduleRunRow.id).where(
                ScheduleRunRow.schedule_id == str(schedule_id),
                ScheduleRunRow.created_at >= aware_utc(since),
                ScheduleRunRow.state.not_in(
                    [
                        ScheduleRunState.PENDING.value,
                        ScheduleRunState.SKIPPED.value,
                        ScheduleRunState.CANCELLED.value,
                    ]
                ),
            )
            if exclude_run_id is not None:
                statement = statement.where(ScheduleRunRow.id != str(exclude_run_id))
            return len(tuple(session.scalars(statement)))

    def disable_after_consecutive_failures(
        self,
        schedule_id: UUID,
        threshold: int,
    ) -> ScheduleDefinition | None:
        failure_states = {
            ScheduleRunState.FAILED.value,
            ScheduleRunState.TIMED_OUT.value,
        }
        with self.database.session() as session:
            row = session.get(ScheduleRow, str(schedule_id))
            if row is None:
                raise KeyError(schedule_id)
            recent_states = tuple(
                session.scalars(
                    select(ScheduleRunRow.state)
                    .where(ScheduleRunRow.schedule_id == row.id)
                    .order_by(ScheduleRunRow.created_at.desc())
                    .limit(threshold)
                )
            )
            if len(recent_states) < threshold or any(
                state not in failure_states for state in recent_states
            ):
                return None
            now = aware_utc(self.clock.now())
            row.status = ScheduleStatus.DISABLED.value
            row.next_run_at = None
            row.review_reason = f"Disabled after {threshold} consecutive failures"
            row.updated_at = now
            self._revoke_authorizations(session, row.id, now)
        disabled = self.get(schedule_id)
        self._record_schedule_event(
            "schedule_disabled",
            disabled,
            "failure_threshold",
            threshold=threshold,
        )
        self._notify_changed()
        return disabled

    def _initial_occurrence(self, definition: ScheduleDefinition, now: datetime) -> datetime | None:
        if isinstance(definition.trigger, OneTimeTrigger):
            return aware_utc(definition.trigger.run_at)
        return next_occurrence(definition.trigger, now)

    @staticmethod
    def _action_for_revision(action, schedule_id: UUID, revision: int, user_id: str):
        def revised_call(call, step_id: str):
            return call.model_copy(
                update={
                    "permission_context": call.permission_context.model_copy(
                        update={
                            "user_id": user_id,
                            "session_id": f"scheduler:{schedule_id}",
                            "schedule_id": schedule_id,
                            "schedule_revision": revision,
                        }
                    ),
                    "idempotency_key": (f"schedule-template:{schedule_id}:{revision}:{step_id}"),
                }
            )

        if isinstance(action, FixedSkillAction):
            return action.model_copy(update={"call": revised_call(action.call, "action")})
        if isinstance(action, FrozenPlanAction):
            steps = tuple(
                step.model_copy(update={"call": revised_call(step.call, step.step_id)})
                for step in action.plan.steps
            )
            return action.model_copy(
                update={"plan": action.plan.model_copy(update={"steps": steps})}
            )
        return action

    def _record_schedule_event(
        self,
        event_type: str,
        schedule: ScheduleDefinition,
        decision: str,
        **details,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            correlation_id=f"schedule:{schedule.schedule_id}:{schedule.revision}",
            event_type=event_type,
            decision=decision,
            details={
                "schedule_id": str(schedule.schedule_id),
                "schedule_revision": schedule.revision,
                "status": schedule.status.value,
                "action_digest": digest(schedule.action),
                **details,
            },
        )

    def _record_run_event(
        self,
        event_type: str,
        run: ScheduleRun,
        decision: str,
        **details,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            correlation_id=str(run.correlation_id or run.run_id),
            event_type=event_type,
            decision=decision,
            details={
                "schedule_id": str(run.schedule_id),
                "schedule_revision": run.schedule_revision,
                "run_id": str(run.run_id),
                "state": run.state.value,
                "scheduled_for": run.scheduled_for.isoformat(),
                "attempt": run.attempt,
                **details,
            },
        )

    @staticmethod
    def _new_schedule_row(
        definition: ScheduleDefinition,
        action_payload: str,
        action_digest: str,
    ) -> ScheduleRow:
        timezone = (
            definition.trigger.timezone
            if isinstance(definition.trigger, RecurrenceTrigger)
            else None
        )
        return ScheduleRow(
            id=str(definition.schedule_id),
            user_id=definition.user_id,
            name=definition.name,
            description=definition.description,
            status=definition.status.value,
            revision=definition.revision,
            trigger_json=canonical_json(definition.trigger),
            trigger_version=1,
            timezone=timezone,
            action_json=action_payload,
            action_version=1,
            action_digest=action_digest,
            autonomy_policy_json=canonical_json(definition.autonomy_policy),
            run_policy_json=canonical_json(definition.run_policy),
            delivery_policy_json=canonical_json(definition.delivery_policy),
            skill_snapshot_json=canonical_json(definition.skill_snapshot),
            next_run_at=definition.next_run_at,
            last_run_at=definition.last_run_at,
            created_at=definition.created_at,
            updated_at=definition.updated_at,
            authorized_at=definition.authorized_at,
            expires_at=definition.autonomy_policy.expires_at,
            paused_reason=definition.paused_reason,
            review_reason=definition.review_reason,
        )

    def _upsert_authorization(
        self,
        session,
        definition: ScheduleDefinition,
        action_digest: str,
        created_at: datetime,
    ) -> None:
        row = session.scalar(
            select(BackgroundAuthorizationRow).where(
                BackgroundAuthorizationRow.schedule_id == str(definition.schedule_id),
                BackgroundAuthorizationRow.schedule_revision == definition.revision,
            )
        )
        values = {
            "user_id": definition.user_id,
            "machine_identity": self.machine_identity,
            "action_digest": action_digest,
            "policy_digest": digest(definition.autonomy_policy),
            "allowed_skill_ids_json": canonical_json(definition.autonomy_policy.allowed_skill_ids),
            "allowed_capabilities_json": canonical_json(
                definition.autonomy_policy.allowed_capabilities
            ),
            "allowed_targets_json": canonical_json(definition.autonomy_policy.allowed_targets),
            "max_risk": definition.autonomy_policy.max_risk.value,
            "created_at": created_at,
            "expires_at": definition.autonomy_policy.expires_at,
            "revoked_at": None,
        }
        if row is None:
            session.add(
                BackgroundAuthorizationRow(
                    id=str(uuid4()),
                    schedule_id=str(definition.schedule_id),
                    schedule_revision=definition.revision,
                    **values,
                )
            )
        else:
            for key, value in values.items():
                setattr(row, key, value)

    @staticmethod
    def _revoke_authorizations(session, schedule_id: str, revoked_at: datetime) -> None:
        session.execute(
            update(BackgroundAuthorizationRow)
            .where(
                BackgroundAuthorizationRow.schedule_id == schedule_id,
                BackgroundAuthorizationRow.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )

    @staticmethod
    def _definition(row: ScheduleRow) -> ScheduleDefinition:
        return ScheduleDefinition(
            schedule_id=UUID(row.id),
            user_id=row.user_id,
            name=row.name,
            description=row.description,
            status=ScheduleStatus(row.status),
            revision=row.revision,
            trigger=TRIGGER_ADAPTER.validate_python(json.loads(row.trigger_json)),
            action=ACTION_ADAPTER.validate_python(json.loads(row.action_json)),
            autonomy_policy=AutonomyPolicy.model_validate(json.loads(row.autonomy_policy_json)),
            run_policy=RunPolicy.model_validate(json.loads(row.run_policy_json)),
            delivery_policy=DeliveryPolicy.model_validate(json.loads(row.delivery_policy_json)),
            skill_snapshot=_SKILL_SNAPSHOT_ADAPTER.validate_python(
                json.loads(row.skill_snapshot_json)
            ),
            next_run_at=aware_utc(row.next_run_at) if row.next_run_at else None,
            last_run_at=aware_utc(row.last_run_at) if row.last_run_at else None,
            created_at=aware_utc(row.created_at),
            updated_at=aware_utc(row.updated_at),
            authorized_at=aware_utc(row.authorized_at) if row.authorized_at else None,
            paused_reason=row.paused_reason,
            review_reason=row.review_reason,
        )

    @staticmethod
    def _run(row: ScheduleRunRow) -> ScheduleRun:
        return ScheduleRun(
            run_id=UUID(row.id),
            schedule_id=UUID(row.schedule_id),
            schedule_revision=row.schedule_revision,
            occurrence_key=row.occurrence_key,
            scheduled_for=aware_utc(row.scheduled_for),
            state=ScheduleRunState(row.state),
            attempt=row.attempt,
            lateness_seconds=row.lateness_seconds,
            lease_owner=UUID(row.lease_owner) if row.lease_owner else None,
            lease_expires_at=aware_utc(row.lease_expires_at) if row.lease_expires_at else None,
            correlation_id=UUID(row.correlation_id) if row.correlation_id else None,
            idempotency_key=row.idempotency_key,
            action_digest=row.action_digest,
            result_status=row.result_status,
            result=json.loads(row.result_json) if row.result_json else None,
            error=json.loads(row.error_json) if row.error_json else None,
            approval_expires_at=(
                aware_utc(row.approval_expires_at) if row.approval_expires_at else None
            ),
            blocked_reason=row.blocked_reason,
            retry_at=aware_utc(row.retry_at) if row.retry_at else None,
            created_at=aware_utc(row.created_at),
            started_at=aware_utc(row.started_at) if row.started_at else None,
            completed_at=aware_utc(row.completed_at) if row.completed_at else None,
        )

    @staticmethod
    def _authorization(row: BackgroundAuthorizationRow) -> BackgroundAuthorization:
        return BackgroundAuthorization(
            authorization_id=UUID(row.id),
            schedule_id=UUID(row.schedule_id),
            schedule_revision=row.schedule_revision,
            user_id=row.user_id,
            machine_identity=row.machine_identity,
            action_digest=row.action_digest,
            policy_digest=row.policy_digest,
            allowed_skill_ids=tuple(json.loads(row.allowed_skill_ids_json)),
            allowed_capabilities=tuple(json.loads(row.allowed_capabilities_json)),
            allowed_targets=tuple(json.loads(row.allowed_targets_json)),
            max_risk=RiskLevel(row.max_risk),
            created_at=aware_utc(row.created_at),
            expires_at=aware_utc(row.expires_at) if row.expires_at else None,
            revoked_at=aware_utc(row.revoked_at) if row.revoked_at else None,
        )
