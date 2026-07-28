from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator
from sqlalchemy import delete, func, select

from assistant.contracts import StrictModel, utcnow
from assistant.storage.database import AuditStore, Database, NotificationRow

logger = logging.getLogger("assistant.scheduling.notifications")

NotificationSeverity = Literal["info", "success", "warning", "error"]
NotificationCategory = Literal[
    "system",
    "reminder",
    "automation",
    "approval",
    "report",
    "media",
]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class NotificationMessage(StrictModel):
    notification_id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    severity: NotificationSeverity = "info"
    category: NotificationCategory = "system"
    schedule_id: UUID | None = None
    run_id: UUID | None = None
    created_at: datetime = Field(default_factory=utcnow)
    read_at: datetime | None = None
    dismissed_at: datetime | None = None

    @field_validator("created_at", "read_at", "dismissed_at")
    @classmethod
    def _datetime_is_aware(cls, value: datetime | None, info) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{info.field_name} must include a time zone")
        return value


class NotificationStore:
    """Durable notification inbox backed by the application database."""

    def __init__(self, database: Database, *, history_limit: int = 1_000) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self.database = database
        self.history_limit = history_limit

    def add(self, message: NotificationMessage) -> NotificationMessage:
        with self.database.session() as session:
            existing = session.get(NotificationRow, str(message.notification_id))
            if existing is None:
                created_at = message.created_at.astimezone(UTC)
                latest = _aware(session.scalar(select(func.max(NotificationRow.created_at))))
                if latest is not None and created_at <= latest:
                    created_at = latest + timedelta(microseconds=1)
                session.add(
                    NotificationRow(
                        id=str(message.notification_id),
                        title=message.title,
                        message=message.message,
                        severity=message.severity,
                        category=message.category,
                        schedule_id=str(message.schedule_id) if message.schedule_id else None,
                        run_id=str(message.run_id) if message.run_id else None,
                        created_at=created_at,
                        read_at=message.read_at.astimezone(UTC) if message.read_at else None,
                        dismissed_at=(
                            message.dismissed_at.astimezone(UTC) if message.dismissed_at else None
                        ),
                    )
                )
        self._prune()
        return self.get(message.notification_id)

    def get(self, notification_id: UUID) -> NotificationMessage:
        with self.database.session() as session:
            row = session.get(NotificationRow, str(notification_id))
            if row is None:
                raise KeyError(notification_id)
            return self._message(row)

    def list(
        self,
        *,
        unread_only: bool = False,
        include_dismissed: bool = False,
        limit: int = 200,
    ) -> tuple[NotificationMessage, ...]:
        if limit < 1:
            return ()
        statement = select(NotificationRow)
        if unread_only:
            statement = statement.where(NotificationRow.read_at.is_(None))
        if not include_dismissed:
            statement = statement.where(NotificationRow.dismissed_at.is_(None))
        statement = statement.order_by(
            NotificationRow.created_at.desc(),
            NotificationRow.id.desc(),
        ).limit(limit)
        with self.database.session() as session:
            return tuple(self._message(row) for row in session.scalars(statement))

    def unread_count(self) -> int:
        statement = select(func.count(NotificationRow.id)).where(
            NotificationRow.read_at.is_(None),
            NotificationRow.dismissed_at.is_(None),
        )
        with self.database.session() as session:
            return int(session.scalar(statement) or 0)

    def mark_read(
        self,
        notification_id: UUID,
        *,
        when: datetime | None = None,
    ) -> NotificationMessage:
        timestamp = _aware(when or utcnow())
        with self.database.session() as session:
            row = session.get(NotificationRow, str(notification_id))
            if row is None:
                raise KeyError(notification_id)
            if row.read_at is None:
                row.read_at = timestamp
        return self.get(notification_id)

    def mark_all_read(self, *, when: datetime | None = None) -> int:
        timestamp = _aware(when or utcnow())
        with self.database.session() as session:
            rows = tuple(
                session.scalars(
                    select(NotificationRow).where(
                        NotificationRow.read_at.is_(None),
                        NotificationRow.dismissed_at.is_(None),
                    )
                )
            )
            for row in rows:
                row.read_at = timestamp
            return len(rows)

    def dismiss(
        self,
        notification_id: UUID,
        *,
        when: datetime | None = None,
    ) -> NotificationMessage:
        timestamp = _aware(when or utcnow())
        with self.database.session() as session:
            row = session.get(NotificationRow, str(notification_id))
            if row is None:
                raise KeyError(notification_id)
            row.read_at = row.read_at or timestamp
            row.dismissed_at = timestamp
        return self.get(notification_id)

    def _prune(self) -> None:
        statement = (
            select(NotificationRow.id)
            .order_by(NotificationRow.created_at.desc(), NotificationRow.id.desc())
            .offset(self.history_limit)
        )
        with self.database.session() as session:
            stale_ids = tuple(session.scalars(statement))
            if stale_ids:
                session.execute(delete(NotificationRow).where(NotificationRow.id.in_(stale_ids)))

    @staticmethod
    def _message(row: NotificationRow) -> NotificationMessage:
        return NotificationMessage(
            notification_id=UUID(row.id),
            title=row.title,
            message=row.message,
            severity=row.severity,
            category=row.category,
            schedule_id=UUID(row.schedule_id) if row.schedule_id else None,
            run_id=UUID(row.run_id) if row.run_id else None,
            created_at=_aware(row.created_at),
            read_at=_aware(row.read_at),
            dismissed_at=_aware(row.dismissed_at),
        )


class NotificationCenter:
    def __init__(
        self,
        store: NotificationStore | None = None,
        history_limit: int = 1_000,
    ) -> None:
        self.store = store
        self._history: deque[NotificationMessage] = deque(maxlen=history_limit)
        self._listeners: set[Callable[[NotificationMessage], None]] = set()
        self._lock = threading.RLock()

    def publish(self, message: NotificationMessage) -> None:
        sanitized = message.model_copy(
            update={
                "title": str(AuditStore.redact(message.title)),
                "message": str(AuditStore.redact(message.message)),
            }
        )
        with self._lock:
            delivered = self.store.add(sanitized) if self.store is not None else sanitized
            if self.store is None:
                self._history.append(delivered)
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(delivered)
            except Exception:
                logger.exception("Notification listener failed")

    def history(self) -> tuple[NotificationMessage, ...]:
        with self._lock:
            if self.store is not None:
                return tuple(
                    reversed(
                        self.store.list(
                            include_dismissed=True,
                            limit=self.store.history_limit,
                        )
                    )
                )
            return tuple(self._history)

    def notifications(
        self,
        *,
        unread_only: bool = False,
        include_dismissed: bool = False,
        limit: int = 200,
    ) -> tuple[NotificationMessage, ...]:
        with self._lock:
            if self.store is not None:
                return self.store.list(
                    unread_only=unread_only,
                    include_dismissed=include_dismissed,
                    limit=limit,
                )
            values = [
                message
                for message in reversed(self._history)
                if (not unread_only or message.read_at is None)
                and (include_dismissed or message.dismissed_at is None)
            ]
            return tuple(values[:limit])

    def unread_count(self) -> int:
        with self._lock:
            if self.store is not None:
                return self.store.unread_count()
            return sum(
                message.read_at is None and message.dismissed_at is None
                for message in self._history
            )

    def get(self, notification_id: UUID) -> NotificationMessage:
        with self._lock:
            if self.store is not None:
                return self.store.get(notification_id)
            for message in self._history:
                if message.notification_id == notification_id:
                    return message
        raise KeyError(notification_id)

    def mark_read(self, notification_id: UUID) -> NotificationMessage:
        with self._lock:
            if self.store is not None:
                return self.store.mark_read(notification_id)
            return self._replace_memory(notification_id, read_at=utcnow())

    def mark_all_read(self) -> int:
        with self._lock:
            if self.store is not None:
                return self.store.mark_all_read()
            changed = 0
            timestamp = utcnow()
            for message in tuple(self._history):
                if message.read_at is None and message.dismissed_at is None:
                    self._replace_memory(message.notification_id, read_at=timestamp)
                    changed += 1
            return changed

    def dismiss(self, notification_id: UUID) -> NotificationMessage:
        with self._lock:
            if self.store is not None:
                return self.store.dismiss(notification_id)
            timestamp = utcnow()
            return self._replace_memory(
                notification_id,
                read_at=timestamp,
                dismissed_at=timestamp,
            )

    def _replace_memory(self, notification_id: UUID, **updates) -> NotificationMessage:
        values = list(self._history)
        for index, message in enumerate(values):
            if message.notification_id == notification_id:
                updated = message.model_copy(update=updates)
                values[index] = updated
                self._history.clear()
                self._history.extend(values)
                return updated
        raise KeyError(notification_id)

    def add_listener(self, listener: Callable[[NotificationMessage], None]) -> None:
        with self._lock:
            self._listeners.add(listener)

    def remove_listener(self, listener: Callable[[NotificationMessage], None]) -> None:
        with self._lock:
            self._listeners.discard(listener)
