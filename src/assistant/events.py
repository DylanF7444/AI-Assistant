from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from uuid import UUID

from assistant.contracts import PipelineEvent

logger = logging.getLogger("assistant.events")


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue[PipelineEvent], asyncio.AbstractEventLoop] = {}
        self._history: dict[UUID, list[PipelineEvent]] = defaultdict(list)
        self._listeners: set[Callable[[PipelineEvent], None]] = set()
        self._lock = threading.RLock()

    async def publish(self, event: PipelineEvent) -> None:
        current_loop = asyncio.get_running_loop()
        with self._lock:
            self._history[event.correlation_id].append(event)
            subscribers = tuple(self._subscribers.items())
            listeners = tuple(self._listeners)
        for queue, loop in subscribers:
            if loop is current_loop:
                queue.put_nowait(event)
            elif loop.is_running():
                loop.call_soon_threadsafe(queue.put_nowait, event)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("Pipeline event listener failed")

    async def subscribe(self) -> AsyncIterator[PipelineEvent]:
        queue: asyncio.Queue[PipelineEvent] = asyncio.Queue()
        with self._lock:
            self._subscribers[queue] = asyncio.get_running_loop()
        try:
            while True:
                yield await queue.get()
        finally:
            with self._lock:
                self._subscribers.pop(queue, None)

    def history(self, correlation_id: UUID) -> tuple[PipelineEvent, ...]:
        with self._lock:
            return tuple(self._history.get(correlation_id, ()))

    def add_listener(self, listener: Callable[[PipelineEvent], None]) -> None:
        with self._lock:
            self._listeners.add(listener)

    def remove_listener(self, listener: Callable[[PipelineEvent], None]) -> None:
        with self._lock:
            self._listeners.discard(listener)
