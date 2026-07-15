from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any

from assistant.scheduling.service import SchedulerService


class BackgroundRuntime:
    def __init__(self, scheduler: SchedulerService) -> None:
        self.scheduler = scheduler
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 5.0) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._thread_main,
                name="assistant-background-runtime",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("Background runtime did not start")

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        loop = self._loop
        if loop is None or not self.running:
            coroutine.close()
            raise RuntimeError("Background runtime is not running")
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def wake(self) -> None:
        self.scheduler.wake()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        self.scheduler.request_stop()
        thread.join(timeout)
        if thread.is_alive():
            raise RuntimeError("Background runtime did not stop")
        with self._lock:
            self._thread = None
            self._loop = None

    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ready.set()
        await self.scheduler.run()
