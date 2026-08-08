from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from assistant.scheduling.clock import Clock, SystemClock
from assistant.scheduling.contracts import (
    EventObservation,
    ScheduleRun,
    ScheduleRunState,
    ScheduleStatus,
)
from assistant.scheduling.runner import BackgroundRunner
from assistant.scheduling.store import ScheduleStore, aware_utc


class EventSource(Protocol):
    async def poll(self) -> int: ...


class SchedulerService:
    def __init__(
        self,
        store: ScheduleStore,
        runner: BackgroundRunner,
        *,
        clock: Clock | None = None,
        enabled: bool = True,
        fallback_wake_seconds: float = 30.0,
        lease_seconds: int = 30,
        max_runs_per_wake: int = 100,
        max_concurrency: int = 1,
        event_sources: tuple[EventSource, ...] = (),
    ) -> None:
        self.store = store
        self.runner = runner
        self.clock = clock or SystemClock()
        self.enabled = enabled
        self.fallback_wake_seconds = fallback_wake_seconds
        self.lease_seconds = lease_seconds
        self.max_runs_per_wake = max_runs_per_wake
        self.max_concurrency = max(1, max_concurrency)
        self.event_sources = event_sources
        self.lease_owner: UUID = uuid4()
        self._wake: asyncio.Event | None = None
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._paused = threading.Event()
        self.store.add_listener(self.wake)

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()
        self.wake()

    def resume(self) -> None:
        self._paused.clear()
        self.wake()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self.store.reconcile_authorization_identity()
        self.store.reconcile(self.clock.now())
        try:
            while not self._stop.is_set():
                if self.enabled and not self.paused:
                    await self.run_once()
                await self._wait_for_next_wake()
        finally:
            self._loop = None
            self._wake = None
            self._stop = None

    async def run_once(self) -> int:
        if not self.enabled or self.paused:
            return 0
        now = aware_utc(self.clock.now())
        for source in self.event_sources:
            await source.poll()
        self.runner.expire_pending_approvals()
        self.store.materialize_due(now)
        completed = 0
        while completed < self.max_runs_per_wake:
            batch = []
            for _ in range(min(self.max_concurrency, self.max_runs_per_wake - completed)):
                run = self.store.claim_next(
                    self.lease_owner,
                    now=aware_utc(self.clock.now()),
                    lease_seconds=self.lease_seconds,
                )
                if run is None:
                    break
                batch.append(run)
            if not batch:
                break
            await asyncio.gather(*(self.runner.run(run) for run in batch))
            completed += len(batch)
        return completed

    def wake(self) -> None:
        loop = self._loop
        event = self._wake
        if loop is None or event is None:
            return
        loop.call_soon_threadsafe(event.set)

    def observe(self, observation: EventObservation) -> tuple[ScheduleRun, ...]:
        runs = self.store.observe(observation)
        if runs:
            self.wake()
        return runs

    def request_stop(self) -> None:
        loop = self._loop
        event = self._stop
        wake = self._wake
        if loop is None or event is None:
            return

        def stop() -> None:
            event.set()
            if wake is not None:
                wake.set()

        loop.call_soon_threadsafe(stop)

    async def health(self) -> dict[str, object]:
        active_schedules = self.store.list(statuses=(ScheduleStatus.ACTIVE,))
        queued_runs = self.store.list_runs(
            states=(ScheduleRunState.PENDING, ScheduleRunState.AWAITING_APPROVAL),
        )
        return {
            "healthy": True,
            "enabled": self.enabled,
            "paused": self.paused,
            "active_schedules": len(active_schedules),
            "queued_runs": len(queued_runs),
            "lease_owner": str(self.lease_owner),
        }

    async def _wait_for_next_wake(self) -> None:
        assert self._wake is not None
        assert self._stop is not None
        self._wake.clear()
        timeout = (
            self.fallback_wake_seconds
            if not self.enabled or self.paused
            else self._timeout_seconds(self.store.next_wake_at())
        )
        wake_task = asyncio.create_task(self._wake.wait())
        stop_task = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            {wake_task, stop_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    def _timeout_seconds(self, next_wake: datetime | None) -> float:
        if next_wake is None:
            return self.fallback_wake_seconds
        delay = (aware_utc(next_wake) - aware_utc(self.clock.now())).total_seconds()
        return min(self.fallback_wake_seconds, max(0.0, delay))
