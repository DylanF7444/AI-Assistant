from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from assistant.contracts import StrictModel, utcnow

logger = logging.getLogger("assistant.scheduling.notifications")


class NotificationMessage(StrictModel):
    notification_id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    severity: Literal["info", "success", "warning", "error"] = "info"
    schedule_id: UUID | None = None
    run_id: UUID | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("created_at")
    @classmethod
    def _created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a time zone")
        return value


class NotificationCenter:
    def __init__(self, history_limit: int = 1_000) -> None:
        self._history: deque[NotificationMessage] = deque(maxlen=history_limit)
        self._listeners: set[Callable[[NotificationMessage], None]] = set()
        self._lock = threading.RLock()

    def publish(self, message: NotificationMessage) -> None:
        with self._lock:
            self._history.append(message)
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(message)
            except Exception:
                logger.exception("Notification listener failed")

    def history(self) -> tuple[NotificationMessage, ...]:
        with self._lock:
            return tuple(self._history)

    def add_listener(self, listener: Callable[[NotificationMessage], None]) -> None:
        with self._lock:
            self._listeners.add(listener)

    def remove_listener(self, listener: Callable[[NotificationMessage], None]) -> None:
        with self._lock:
            self._listeners.discard(listener)
