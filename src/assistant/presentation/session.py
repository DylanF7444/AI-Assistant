from __future__ import annotations

import threading
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from assistant.contracts import MediaSource, StrictModel


class MediaSessionStatus(StrEnum):
    PREPARED = "prepared"
    LOADING = "loading"
    READY = "ready"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOSED = "closed"


class MediaSessionSnapshot(StrictModel):
    presentation_id: UUID
    source: MediaSource
    status: MediaSessionStatus = MediaSessionStatus.PREPARED
    position_seconds: float = Field(default=0, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    volume: float = Field(default=1, ge=0, le=1)
    muted: bool = False
    fullscreen: bool = False
    error_code: str | None = Field(default=None, max_length=100)


class MediaSessionRegistry:
    """Thread-safe in-memory state shared by orchestration and the Qt host."""

    def __init__(self) -> None:
        self._snapshot: MediaSessionSnapshot | None = None
        self._lock = threading.RLock()

    def prepare(self, source: MediaSource) -> MediaSessionSnapshot:
        snapshot = MediaSessionSnapshot(
            presentation_id=uuid4(),
            source=source,
        )
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def current(self) -> MediaSessionSnapshot | None:
        with self._lock:
            return self._snapshot

    def active_presentation_id(self) -> UUID | None:
        snapshot = self.current()
        if snapshot is None or snapshot.status in {
            MediaSessionStatus.FAILED,
            MediaSessionStatus.CLOSED,
        }:
            return None
        return snapshot.presentation_id

    def update(
        self,
        presentation_id: UUID,
        *,
        status: MediaSessionStatus | None = None,
        position_seconds: float | None = None,
        duration_seconds: float | None = None,
        volume: float | None = None,
        muted: bool | None = None,
        fullscreen: bool | None = None,
        error_code: str | None = None,
    ) -> MediaSessionSnapshot:
        with self._lock:
            if self._snapshot is None or self._snapshot.presentation_id != presentation_id:
                raise ValueError("The media presentation is no longer active")
            changes = {
                key: value
                for key, value in {
                    "status": status,
                    "position_seconds": position_seconds,
                    "duration_seconds": duration_seconds,
                    "volume": volume,
                    "muted": muted,
                    "fullscreen": fullscreen,
                    "error_code": error_code,
                }.items()
                if value is not None
            }
            self._snapshot = MediaSessionSnapshot.model_validate(
                {**self._snapshot.model_dump(), **changes}
            )
            return self._snapshot

    def close(self, presentation_id: UUID | None = None) -> None:
        with self._lock:
            if self._snapshot is None:
                return
            if presentation_id is not None and self._snapshot.presentation_id != presentation_id:
                return
            self._snapshot = self._snapshot.model_copy(update={"status": MediaSessionStatus.CLOSED})

    def clear(self) -> None:
        with self._lock:
            self._snapshot = None
