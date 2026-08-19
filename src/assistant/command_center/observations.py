from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select

from assistant.command_center.contracts import ObservationCursor
from assistant.storage.database import CommandCenterObservationCursorRow, Database


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CommandCenterObservationStore:
    """Persists metadata-only fingerprints and polling backoff state."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(
        self,
        schedule_id: UUID,
        schedule_revision: int,
        binding_id: UUID,
    ) -> ObservationCursor | None:
        with self.database.session() as session:
            row = session.get(
                CommandCenterObservationCursorRow,
                (str(schedule_id), schedule_revision, str(binding_id)),
            )
            return None if row is None else self._cursor(row)

    def list(self) -> tuple[ObservationCursor, ...]:
        with self.database.session() as session:
            rows = session.scalars(
                select(CommandCenterObservationCursorRow).order_by(
                    CommandCenterObservationCursorRow.next_poll_at,
                    CommandCenterObservationCursorRow.schedule_id,
                )
            )
            return tuple(self._cursor(row) for row in rows)

    def record_success(
        self,
        *,
        schedule_id: UUID,
        schedule_revision: int,
        binding_id: UUID,
        provider_id: str,
        fingerprint: str,
        interval_seconds: int,
        now: datetime,
    ) -> tuple[ObservationCursor, str | None]:
        current_time = _aware(now)
        key = (str(schedule_id), schedule_revision, str(binding_id))
        with self.database.session() as session:
            row = session.get(CommandCenterObservationCursorRow, key)
            previous = row.fingerprint if row is not None else None
            if row is None:
                row = CommandCenterObservationCursorRow(
                    schedule_id=key[0],
                    schedule_revision=schedule_revision,
                    binding_id=key[2],
                    provider_id=provider_id,
                    failure_count=0,
                    next_poll_at=current_time,
                    updated_at=current_time,
                )
                session.add(row)
            row.provider_id = provider_id
            row.fingerprint = fingerprint
            row.failure_count = 0
            row.last_error_code = None
            row.last_polled_at = current_time
            row.next_poll_at = current_time + timedelta(seconds=interval_seconds)
            row.updated_at = current_time
        cursor = self.get(schedule_id, schedule_revision, binding_id)
        assert cursor is not None
        return cursor, previous

    def record_failure(
        self,
        *,
        schedule_id: UUID,
        schedule_revision: int,
        binding_id: UUID,
        provider_id: str,
        error_code: str,
        minimum_interval_seconds: int,
        now: datetime,
    ) -> ObservationCursor:
        current_time = _aware(now)
        key = (str(schedule_id), schedule_revision, str(binding_id))
        with self.database.session() as session:
            row = session.get(CommandCenterObservationCursorRow, key)
            if row is None:
                row = CommandCenterObservationCursorRow(
                    schedule_id=key[0],
                    schedule_revision=schedule_revision,
                    binding_id=key[2],
                    provider_id=provider_id,
                    failure_count=0,
                    next_poll_at=current_time,
                    updated_at=current_time,
                )
                session.add(row)
            row.provider_id = provider_id
            row.failure_count += 1
            row.last_error_code = error_code[:100]
            row.last_polled_at = current_time
            backoff = min(3_600, minimum_interval_seconds * (2 ** (row.failure_count - 1)))
            row.next_poll_at = current_time + timedelta(seconds=backoff)
            row.updated_at = current_time
        cursor = self.get(schedule_id, schedule_revision, binding_id)
        assert cursor is not None
        return cursor

    def clear_inactive(self, active: set[tuple[str, int, str]]) -> int:
        with self.database.session() as session:
            rows = tuple(session.scalars(select(CommandCenterObservationCursorRow)))
            removed = 0
            for row in rows:
                key = (row.schedule_id, row.schedule_revision, row.binding_id)
                if key not in active:
                    session.delete(row)
                    removed += 1
            return removed

    def clear_binding(self, binding_id: UUID) -> int:
        with self.database.session() as session:
            result = session.execute(
                delete(CommandCenterObservationCursorRow).where(
                    CommandCenterObservationCursorRow.binding_id == str(binding_id)
                )
            )
            return int(result.rowcount or 0)

    def clear(self) -> int:
        with self.database.session() as session:
            result = session.execute(delete(CommandCenterObservationCursorRow))
            return int(result.rowcount or 0)

    @staticmethod
    def _cursor(row: CommandCenterObservationCursorRow) -> ObservationCursor:
        return ObservationCursor(
            schedule_id=UUID(row.schedule_id),
            schedule_revision=row.schedule_revision,
            binding_id=UUID(row.binding_id),
            provider_id=row.provider_id,
            fingerprint=row.fingerprint,
            failure_count=row.failure_count,
            last_error_code=row.last_error_code,
            last_polled_at=_aware(row.last_polled_at) if row.last_polled_at else None,
            next_poll_at=_aware(row.next_poll_at),
            updated_at=_aware(row.updated_at),
        )
