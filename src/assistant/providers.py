from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from assistant.contracts import AudioSession, PipelineEvent, Response, Transcript


class STTProvider(Protocol):
    async def transcribe(
        self, session: AudioSession, audio: AsyncIterator[bytes]
    ) -> AsyncIterator[Transcript]: ...

    async def health(self) -> dict[str, Any]: ...

    async def cancel(self) -> None: ...


class LLMProvider(Protocol):
    async def structured_completion(
        self, prompt: str, schema: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]: ...

    async def health(self) -> dict[str, Any]: ...

    async def cancel(self) -> None: ...


class TTSProvider(Protocol):
    async def speak(self, response: Response) -> AsyncIterator[PipelineEvent]: ...

    async def health(self) -> dict[str, Any]: ...

    async def cancel(self) -> None: ...


class WakeWordProvider(Protocol):
    async def detections(self) -> AsyncIterator[float]: ...
    async def cancel(self) -> None: ...


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

