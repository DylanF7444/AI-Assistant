from __future__ import annotations

import asyncio
import array
import math
import time
import wave
from datetime import UTC, datetime
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    input_channels: int
    output_channels: int
    default_sample_rate: float


def enumerate_devices() -> tuple[AudioDevice, ...]:
    try:
        import sounddevice as sd
    except ImportError:
        return ()
    return tuple(
        AudioDevice(
            index=index,
            name=item["name"],
            input_channels=item["max_input_channels"],
            output_channels=item["max_output_channels"],
            default_sample_rate=item["default_samplerate"],
        )
        for index, item in enumerate(sd.query_devices())
    )


class AudioCapture:
    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        device: int | None = None,
        silence_seconds: float = 1.0,
        max_seconds: float = 30,
        pre_roll_seconds: float = 0.3,
        vad_threshold: float = 0.015,
        noise_suppression: bool = True,
        automatic_gain_control: bool = True,
        diagnostic_dir: Path | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.silence_seconds = silence_seconds
        self.max_seconds = max_seconds
        self.pre_roll_seconds = pre_roll_seconds
        self.vad_threshold = vad_threshold
        self.noise_suppression = noise_suppression
        self.automatic_gain_control = automatic_gain_control
        self.diagnostic_dir = diagnostic_dir
        self.level = 0.0
        self._stop = asyncio.Event()
        self._playback_active = False

    @staticmethod
    def rms(pcm: bytes) -> float:
        values = array.array("h")
        values.frombytes(pcm)
        if not values:
            return 0
        return math.sqrt(sum(sample * sample for sample in values) / len(values)) / 32768

    def process(self, pcm: bytes) -> bytes:
        values = array.array("h")
        values.frombytes(pcm)
        if not values:
            return pcm
        level = self.rms(pcm)
        if self.noise_suppression and level < self.vad_threshold * 0.45:
            return b"\x00" * len(pcm)
        if self.automatic_gain_control and level > 0:
            target = 0.12
            gain = min(4.0, max(0.5, target / level))
            for index, sample in enumerate(values):
                values[index] = max(-32768, min(32767, int(sample * gain)))
        return values.tobytes()

    def set_playback_active(self, active: bool) -> None:
        self._playback_active = active

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("Install the 'audio' optional dependency") from exc
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        blocksize = int(self.sample_rate * 0.03)
        pre_roll = deque(
            maxlen=max(1, int(self.pre_roll_seconds * self.sample_rate / blocksize))
        )
        started = speaking = False
        diagnostic = bytearray()
        last_voice = start_time = time.monotonic()
        self._stop.clear()

        def callback(indata: Any, frames: int, timing: Any, status: Any) -> None:
            payload = bytes(indata)
            loop.call_soon_threadsafe(_enqueue, payload)

        def _enqueue(payload: bytes) -> None:
            if not queue.full():
                queue.put_nowait(payload)

        try:
            with sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=blocksize,
                device=self.device,
                channels=1,
                dtype="int16",
                callback=callback,
            ):
                while not self._stop.is_set():
                    payload = await queue.get()
                    if self._playback_active:
                        pre_roll.clear()
                        continue
                    payload = self.process(payload)
                    now = time.monotonic()
                    self.level = self.rms(payload)
                    voice = self.level >= self.vad_threshold
                    if not started:
                        pre_roll.append(payload)
                        if voice:
                            started = speaking = True
                            start_time = last_voice = now
                            while pre_roll:
                                chunk = pre_roll.popleft()
                                diagnostic.extend(chunk)
                                yield chunk
                        continue
                    diagnostic.extend(payload)
                    yield payload
                    if voice:
                        speaking = True
                        last_voice = now
                    if speaking and now - last_voice >= self.silence_seconds:
                        break
                    if now - start_time >= self.max_seconds:
                        break
        except Exception as exc:
            device = "default" if self.device is None else str(self.device)
            raise RuntimeError(
                f"Unable to open or read microphone device {device}: {exc}"
            ) from exc
        finally:
            if self.diagnostic_dir and diagnostic:
                self.diagnostic_dir.mkdir(parents=True, exist_ok=True)
                name = (
                    datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                    + f"-{uuid4().hex[:8]}.wav"
                )
                with wave.open(str(self.diagnostic_dir / name), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(self.sample_rate)
                    output.writeframes(diagnostic)

    async def stop(self) -> None:
        self._stop.set()

    async def microphone_test(self, seconds: float = 2) -> float:
        levels = []

        async def collect():
            async for _ in self.chunks():
                levels.append(self.level)
                if len(levels) * 0.03 >= seconds:
                    await self.stop()

        await collect()
        return max(levels, default=0)
