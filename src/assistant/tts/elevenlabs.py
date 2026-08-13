from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import threading
import wave
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

from assistant.contracts import PipelineEvent, PipelineState, Response

_API_ROOT = "https://api.elevenlabs.io/v1"
_OUTPUT_FORMAT = "pcm_24000"
_SAMPLE_RATE = 24_000
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


class _SynthesisCancelled(Exception):
    pass


class ElevenLabsProvider:
    """ElevenLabs text-to-speech with local playback and bounded audio caching."""

    def __init__(
        self,
        api_key: str | Callable[[], str | None],
        voice_id: str,
        model_id: str = "eleven_flash_v2_5",
        output_device: str | None = None,
        *,
        volume: float = 1.0,
        timeout_seconds: float = 30.0,
        cache_dir: Path | None = None,
        playback_state=None,
        offline_lock: bool = False,
        opener=None,
    ) -> None:
        self._api_key_source = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self.output_device = output_device
        self.volume = volume
        self.timeout_seconds = timeout_seconds
        self.cache_dir = cache_dir
        self.playback_state = playback_state
        self.offline_lock = offline_lock
        self.opener = opener
        self._cancel_event = threading.Event()

    async def speak(self, response: Response) -> AsyncIterator[PipelineEvent]:
        if not response.speech_text:
            return
        if self.offline_lock:
            raise RuntimeError("ElevenLabs speech is blocked by the global offline lock")
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError("An ElevenLabs API key is not configured")
        self._cancel_event.clear()
        cacheable = len(response.speech_text) <= 80 and self.cache_dir is not None
        if cacheable:
            key = hashlib.sha256(
                (
                    f"elevenlabs|{self.voice_id}|{self.model_id}|"
                    f"{_OUTPUT_FORMAT}|{response.speech_text}"
                ).encode()
            ).hexdigest()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            output = self.cache_dir / f"{key}.wav"
        else:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                output = Path(handle.name)
        try:
            if not output.exists() or output.stat().st_size == 0:
                pcm = await asyncio.to_thread(
                    self._request_pcm,
                    response.speech_text,
                    api_key,
                )
                self._write_wave(output, pcm)
            if self._cancel_event.is_set():
                return
            yield PipelineEvent(
                correlation_id=__import__("uuid").uuid4(),
                state=PipelineState.SPEAKING,
                message="Speaking",
            )
            if self.playback_state:
                self.playback_state(True)
            await asyncio.to_thread(self._play, output)
        except _SynthesisCancelled:
            return
        finally:
            if self.playback_state:
                self.playback_state(False)
            if not cacheable:
                output.unlink(missing_ok=True)

    def _api_key(self) -> str | None:
        value = self._api_key_source() if callable(self._api_key_source) else self._api_key_source
        normalized = str(value or "").strip()
        return normalized or None

    def _request_pcm(self, text: str, api_key: str) -> bytes:
        query = urlencode({"output_format": _OUTPUT_FORMAT})
        url = f"{_API_ROOT}/text-to-speech/{quote(self.voice_id, safe='')}?{query}"
        request = Request(
            url,
            data=json.dumps({"text": text, "model_id": self.model_id}).encode("utf-8"),
            headers={
                "Accept": "audio/pcm",
                "Content-Type": "application/json",
                "User-Agent": "LocalAssistant/0.1",
                "xi-api-key": api_key,
            },
            method="POST",
        )
        try:
            with (self.opener or build_opener()).open(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                chunks: list[bytes] = []
                total = 0
                while True:
                    if self._cancel_event.is_set():
                        raise _SynthesisCancelled
                    chunk = response.read(min(64 * 1024, _MAX_AUDIO_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > _MAX_AUDIO_BYTES:
                        raise RuntimeError("ElevenLabs audio exceeded the 25 MB safety limit")
        except HTTPError as error:
            raise RuntimeError(
                f"ElevenLabs speech request failed with HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"ElevenLabs speech request failed: {error}") from error
        pcm = b"".join(chunks)
        if not pcm:
            raise RuntimeError("ElevenLabs returned no audio")
        if len(pcm) % 2:
            raise RuntimeError("ElevenLabs returned invalid PCM audio")
        return pcm

    @staticmethod
    def _write_wave(path: Path, pcm: bytes) -> None:
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(_SAMPLE_RATE)
            audio.writeframes(pcm)

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

            winsound.PlaySound(
                str(path),
                winsound.SND_FILENAME | winsound.SND_ASYNC,
            )
            duration = 0.0
            with wave.open(str(path), "rb") as audio:
                duration = audio.getnframes() / max(audio.getframerate(), 1)
            self._cancel_event.wait(duration)
            if self._cancel_event.is_set():
                winsound.PlaySound(None, winsound.SND_PURGE)

    def set_offline_lock(self, enabled: bool) -> None:
        self.offline_lock = enabled
        if enabled:
            self._cancel_event.set()

    async def cancel(self) -> None:
        self._cancel_event.set()
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except ImportError:
            pass
        try:
            import sounddevice as sd

            sd.stop()
        except ImportError:
            pass

    async def health(self) -> dict[str, Any]:
        try:
            key_configured = bool(self._api_key())
        except Exception:
            key_configured = False
        return {
            "healthy": key_configured and not self.offline_lock,
            "provider": "elevenlabs",
            "voice_id": self.voice_id,
            "model_id": self.model_id,
            "api_key_configured": key_configured,
            "offline_lock": self.offline_lock,
        }
