from __future__ import annotations

from assistant.bootstrap import Container
from assistant.contracts import Response
from assistant.execution.engine import ExecutionOutcome


class DesktopController:
    """Typed boundary between Qt widgets and application services."""

    def __init__(self, container: Container) -> None:
        self.container = container

    async def run_text(self, text: str) -> tuple[Response, ExecutionOutcome]:
        return await self.container.orchestrator.handle_text(text)

    async def cancel(self) -> None:
        await self.container.audio_capture.stop()
        await self.container.stt.cancel()
        await self.container.orchestrator.cancel()
