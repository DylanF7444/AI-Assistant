from __future__ import annotations

import array
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any


class DisabledWakeWordProvider:
    """Explicit no-op provider; passive listening is disabled by default."""

    async def detections(self) -> AsyncIterator[float]:
        if False:
            yield 0.0  # pragma: no cover

    async def cancel(self) -> None:
        return None


class OpenWakeWordProvider:
    """Local passive detector using explicit model files; never downloads."""

    def __init__(
        self,
        model_path: Path,
        *,
        threshold: float = 0.5,
        device: int | None = None,
        sample_rate: int = 16000,
    ) -> None:
        self.model_path = model_path
        self.threshold = threshold
        self.device = device
        self.sample_rate = sample_rate
        self._model: Any = None
        self._stop = asyncio.Event()

    def _load(self) -> None:
        if self._model is not None:
            return
        if not self.model_path.is_file():
            raise FileNotFoundError(self.model_path)
        from openwakeword.model import Model

        self._model = Model(
            wakeword_models=[str(self.model_path)],
            inference_framework="onnx",
            enable_speex_noise_suppression=True,
            vad_threshold=0.5,
        )

    async def detections(self) -> AsyncIterator[float]:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("Install the 'audio' optional dependency") from exc
        await asyncio.to_thread(self._load)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=20)
        self._stop.clear()

        def callback(indata, _frames, _timing, _status):
            payload = bytes(indata)
            loop.call_soon_threadsafe(enqueue, payload)

        def enqueue(payload: bytes):
            if not queue.full():
                queue.put_nowait(payload)

        with sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=1280,
            device=self.device,
            channels=1,
            dtype="int16",
            callback=callback,
        ):
            while not self._stop.is_set():
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=0.25)
                except TimeoutError:
                    continue
                samples = array.array("h")
                samples.frombytes(payload)
                prediction = self._model.predict(samples)
                score = max(
                    (
                        float(value[-1] if hasattr(value, "__len__") else value)
                        for value in prediction.values()
                    ),
                    default=0.0,
                )
                if score >= self.threshold:
                    yield score

    async def cancel(self) -> None:
        self._stop.set()
