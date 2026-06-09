from __future__ import annotations

import asyncio
import hashlib
import tempfile
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
    ) -> None:
        self.executable = executable
        self.model = model
        self.output_device = output_device
        self.speed = speed
        self.volume = volume
        self.cache_dir = cache_dir
        self.playback_state = playback_state
        self._process: asyncio.subprocess.Process | None = None

    async def speak(self, response: Response) -> AsyncIterator[PipelineEvent]:
        if not response.speech_text:
            return
        cacheable = len(response.speech_text) <= 80 and self.cache_dir is not None
        if cacheable:
            key = hashlib.sha256(
                f"{self.model.resolve()}|{self.speed}|{response.speech_text}".encode()
            ).hexdigest()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            output = self.cache_dir / f"{key}.wav"
        else:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                output = Path(handle.name)
        try:
            if not output.exists():
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
                _, stderr = await self._process.communicate(
                    response.speech_text.encode("utf-8")
                )
                if self._process.returncode:
                    raise RuntimeError(stderr.decode("utf-8", errors="replace"))
            yield PipelineEvent(
                correlation_id=__import__("uuid").uuid4(),
                state=PipelineState.SPEAKING,
                message="Speaking",
            )
            if self.playback_state:
                self.playback_state(True)
            await asyncio.to_thread(self._play, output)
        finally:
            if self.playback_state:
                self.playback_state(False)
            self._process = None
            if not cacheable:
                output.unlink(missing_ok=True)

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
