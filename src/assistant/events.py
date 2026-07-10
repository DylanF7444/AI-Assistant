from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from uuid import UUID

from assistant.contracts import PipelineEvent


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[PipelineEvent]] = set()
        self._history: dict[UUID, list[PipelineEvent]] = defaultdict(list)
        self._listeners: set[Callable[[PipelineEvent], None]] = set()

    async def publish(self, event: PipelineEvent) -> None:
        self._history[event.correlation_id].append(event)
        for queue in tuple(self._subscribers):
            queue.put_nowait(event)
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:
                continue

    async def subscribe(self) -> AsyncIterator[PipelineEvent]:
        queue: asyncio.Queue[PipelineEvent] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    def history(self, correlation_id: UUID) -> tuple[PipelineEvent, ...]:
        return tuple(self._history.get(correlation_id, ()))

    def add_listener(self, listener: Callable[[PipelineEvent], None]) -> None:
        self._listeners.add(listener)

    def remove_listener(self, listener: Callable[[PipelineEvent], None]) -> None:
        self._listeners.discard(listener)
