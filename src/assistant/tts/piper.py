from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from assistant.contracts import PipelineEvent, PipelineState, Response


class PiperProvider:
    def __init__(
        self,
        executable: Path,
        model: Path,
        output_device: str | None = None,
        *,
        speed: float = 1.0,
        volume: float = 1.0,
        cache_dir: Path | None = None,
        playback_state=None,
        cache_max_bytes: int = 100 * 1024 * 1024,
        cache_retention_days: int = 7,
    ) -> None:
        self.executable = executable
        self.model = model
        self.output_device = output_device
        self.speed = speed
        self.volume = volume
        self.cache_dir = cache_dir
        self.playback_state = playback_state
        self.cache_max_bytes = cache_max_bytes
        self.cache_retention_days = cache_retention_days
        self._process: asyncio.subprocess.Process | None = None

    async def speak(self, response: Response) -> AsyncIterator[PipelineEvent]:
        if not response.speech_text:
            return
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._prune_cache)
        chunks = self._speech_chunks(response.speech_text)
        first_audio = True
        if self.playback_state:
            self.playback_state(True)
        try:
            for chunk in chunks:
                output, cacheable = self._output_path(chunk)
                try:
                    if not output.exists() or output.stat().st_size == 0:
                        await self._synthesize(chunk, output)
                    if first_audio:
                        first_audio = False
                        yield PipelineEvent(
                            correlation_id=__import__("uuid").uuid4(),
                            state=PipelineState.SPEAKING,
                            message="Speaking",
                        )
                    await asyncio.to_thread(self._play, output)
                finally:
                    if not cacheable:
                        output.unlink(missing_ok=True)
        finally:
            if self.playback_state:
                self.playback_state(False)
            self._process = None

    @staticmethod
    def _speech_chunks(text: str, *, maximum_characters: int = 180) -> tuple[str, ...]:
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", text)
            if sentence.strip()
        ]
        chunks: list[str] = []
        for sentence in sentences:
            while len(sentence) > maximum_characters:
                boundary = sentence.rfind(" ", 0, maximum_characters + 1)
                boundary = boundary if boundary >= maximum_characters // 2 else maximum_characters
                chunks.append(sentence[:boundary].strip())
                sentence = sentence[boundary:].strip()
            if sentence:
                chunks.append(sentence)
        return tuple(chunks)

    def _output_path(self, text: str) -> tuple[Path, bool]:
        cacheable = len(text) <= 80 and self.cache_dir is not None
        if cacheable:
            key = hashlib.sha256(f"{self.model.resolve()}|{self.speed}|{text}".encode()).hexdigest()
            return self.cache_dir / f"{key}.wav", True
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            output = Path(handle.name)
        output.unlink(missing_ok=True)
        return output, False

    async def _synthesize(self, text: str, output: Path) -> None:
        self._process = await asyncio.create_subprocess_exec(
            str(self.executable),
            "--model",
            str(self.model),
            "--length_scale",
            str(1 / self.speed),
            "--output_file",
            str(output),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await self._process.communicate(text.encode("utf-8"))
        if self._process.returncode:
            output.unlink(missing_ok=True)
            raise RuntimeError(stderr.decode("utf-8", errors="replace"))

    def _prune_cache(self) -> None:
        if self.cache_dir is None or not self.cache_dir.exists():
            return
        files = [path for path in self.cache_dir.glob("*.wav") if path.is_file()]
        cutoff = time.time() - self.cache_retention_days * 86_400
        for path in files:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        files = sorted(
            (path for path in self.cache_dir.glob("*.wav") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        retained_bytes = 0
        for path in files:
            try:
                retained_bytes += path.stat().st_size
                if retained_bytes > self.cache_max_bytes:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _play(self, path: Path) -> None:
        try:
            import numpy as np
            import sounddevice as sd

            with wave.open(str(path), "rb") as audio:
                frames = audio.readframes(audio.getnframes())
                sample_rate = audio.getframerate()
                channels = audio.getnchannels()
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
            samples = (samples * self.volume / 32768.0).reshape(-1, channels)
            sd.play(samples, sample_rate, device=self.output_device, blocking=True)
        except (ImportError, RuntimeError):
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME)

    async def cancel(self) -> None:
        if self._process:
            self._process.terminate()
        import winsound

        winsound.PlaySound(None, winsound.SND_PURGE)
        try:
            import sounddevice as sd

            sd.stop()
        except ImportError:
            pass

    async def health(self) -> dict[str, Any]:
        return {
            "healthy": self.executable.exists() and self.model.exists(),
            "executable": str(self.executable),
            "model": str(self.model),
        }
