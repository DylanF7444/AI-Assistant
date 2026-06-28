from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import tempfile
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from assistant.contracts import AudioSession, Transcript, WordTimestamp
from assistant.router.normalization import normalize_text


class FasterWhisperProvider:
    def __init__(
        self,
        model_name: str = "small.en",
        device: str = "auto",
        *,
        partial_interval_seconds: float = 1.5,
        partial_min_audio_seconds: float = 1.0,
    ) -> None:
        self.model_name = model_name
        self.requested_device = device
        self._model: Any = None
        self._cancelled = asyncio.Event()
        self.partial_interval_seconds = partial_interval_seconds
        self.partial_min_audio_seconds = partial_min_audio_seconds

    def _model_is_local(self) -> bool:
        return Path(self.model_name).expanduser().exists()

    def _cached_model_exists(self) -> bool:
        cache_root = Path.home() / ".cache" / "huggingface" / "hub"
        model_dir = (
            cache_root
            / f"models--Systran--faster-whisper-{self.model_name.replace('/', '--')}"
            / "snapshots"
        )
        return model_dir.is_dir() and any(model_dir.iterdir())

    def _cuda_runtime_available(self) -> bool:
        if os.name != "nt":
            return True
        try:
            ctypes.WinDLL("cublas64_12.dll")
        except OSError:
            return False
        return True

    async def _load(self) -> None:
        if self._model is not None:
            return
        health = await self.health()
        if not health["healthy"]:
            raise RuntimeError(health["error"])

        def load():
            from faster_whisper import WhisperModel

            candidates = {
                "auto": [("cuda", "int8_float16"), ("cpu", "int8")],
                "cuda": [("cuda", "int8_float16")],
                "cpu": [("cpu", "int8")],
            }[self.requested_device]
            if not self._cuda_runtime_available():
                if self.requested_device == "cuda":
                    raise RuntimeError(
                        "CUDA speech recognition was selected, but cublas64_12.dll "
                        "is not available. Install the CUDA 12 runtime or switch "
                        "the STT device to CPU/auto in Settings."
                    )
                candidates = [
                    (device, compute_type)
                    for device, compute_type in candidates
                    if device != "cuda"
                ]
            errors = []
            for device, compute_type in candidates:
                try:
                    return WhisperModel(
                        self.model_name, device=device, compute_type=compute_type
                    )
                except Exception as exc:
                    errors.append(f"{device}/{compute_type}: {exc}")
            detail = "; ".join(errors) if errors else "no compatible device candidates"
            raise RuntimeError(f"Unable to load faster-whisper ({detail})")

        self._model = await asyncio.to_thread(load)

    async def transcribe(
        self, session: AudioSession, audio: AsyncIterator[bytes]
    ) -> AsyncIterator[Transcript]:
        await self._load()
        self._cancelled.clear()
        chunks = bytearray()
        complete = asyncio.Event()
        producer_error: Exception | None = None

        async def consume() -> None:
            nonlocal producer_error
            try:
                async for chunk in audio:
                    if self._cancelled.is_set():
                        return
                    chunks.extend(chunk)
            except Exception as exc:
                producer_error = exc
            finally:
                complete.set()

        producer = asyncio.create_task(consume())
        minimum_bytes = int(
            session.sample_rate
            * session.channels
            * 2
            * self.partial_min_audio_seconds
        )
        last_partial_size = 0
        try:
            while not complete.is_set():
                try:
                    await asyncio.wait_for(
                        complete.wait(), timeout=self.partial_interval_seconds
                    )
                except TimeoutError:
                    pass
                if (
                    not complete.is_set()
                    and len(chunks) >= minimum_bytes
                    and len(chunks) > last_partial_size
                ):
                    partial = await self._decode(session, bytes(chunks))
                    yield partial.model_copy(update={"is_final": False})
                    last_partial_size = len(chunks)
            await producer
            if producer_error:
                raise producer_error
            if self._cancelled.is_set() or not chunks:
                return
            yield await self._decode(session, bytes(chunks))
        finally:
            if not producer.done():
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

    async def _decode(
        self, session: AudioSession, chunks: bytes
    ) -> Transcript:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = Path(handle.name)
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(session.channels)
                wav.setsampwidth(2)
                wav.setframerate(session.sample_rate)
                wav.writeframes(chunks)

            def run():
                return self._model.transcribe(
                    str(path), beam_size=3, word_timestamps=True, vad_filter=True
                )

            segments, info = await asyncio.to_thread(run)
            materialized = list(segments)
            text = "".join(item.text for item in materialized).strip()
            words = tuple(
                WordTimestamp(
                    word=word.word.strip(),
                    start_seconds=word.start,
                    end_seconds=word.end,
                    confidence=word.probability,
                )
                for segment in materialized
                for word in (segment.words or ())
            )
            confidence = (
                sum(word.confidence or 0 for word in words) / len(words)
                if words
                else None
            )
            return Transcript(
                session_id=session.session_id,
                raw_text=text,
                normalized_text=normalize_text(text),
                confidence=confidence,
                language=info.language,
                words=words,
                is_final=True,
            )
        finally:
            path.unlink(missing_ok=True)

    async def cancel(self) -> None:
        self._cancelled.set()

    async def health(self) -> dict[str, Any]:
        if importlib.util.find_spec("faster_whisper") is None:
            return {
                "healthy": False,
                "loaded": False,
                "model": self.model_name,
                "error": (
                    "Speech-to-text support is not installed. Reinstall the "
                    "application with the STT component."
                ),
            }
        if not self._model_is_local() and not self._cached_model_exists():
            return {
                "healthy": False,
                "loaded": False,
                "model": self.model_name,
                "error": (
                    f"The local STT model '{self.model_name}' is not installed. "
                    "Select an existing faster-whisper model folder in Settings."
                ),
            }
        return {
            "healthy": True,
            "loaded": self._model is not None,
            "model": self.model_name,
        }
