from __future__ import annotations

import array
import asyncio
import math
import re
import time
import wave
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from assistant.audio.activation import ClapDetector


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    input_channels: int
    output_channels: int
    default_sample_rate: float
    host_api: str = ""


@dataclass(frozen=True)
class DisplayAudioDevice:
    index: int
    label: str
    input_channels: int
    output_channels: int
    default_sample_rate: float


def enumerate_devices() -> tuple[AudioDevice, ...]:
    try:
        import sounddevice as sd
    except ImportError:
        return ()
    host_apis = sd.query_hostapis()
    return tuple(
        AudioDevice(
            index=index,
            name=item["name"],
            input_channels=item["max_input_channels"],
            output_channels=item["max_output_channels"],
            default_sample_rate=item["default_samplerate"],
            host_api=host_apis[item["hostapi"]]["name"],
        )
        for index, item in enumerate(sd.query_devices())
    )


_DEVICE_NAME_SUFFIX = re.compile(r"\s*\((\d+)-\s*(in|out)\)\s*$", re.IGNORECASE)
_COMMON_DEVICE_WRAPPER = re.compile(
    r"^(headset microphone|headset|microphone|speakers?)\s+\(([^)]+)\)$",
    re.IGNORECASE,
)
_BLUETOOTH_INSTANCE_NAME = re.compile(r";\(([^)]+)\)")
_BLUETOOTH_ROLE = re.compile(r"hands-free(?:\s+hf\s+audio)?", re.IGNORECASE)
_NOISY_PREFIX = re.compile(
    r"^(headset microphone|headset|microphone|input)\s+",
    re.IGNORECASE,
)
_PSEUDO_INPUT_NAMES = {
    "microsoft sound mapper - input",
    "primary sound capture driver",
}
_PSEUDO_OUTPUT_NAMES = {
    "microsoft sound mapper - output",
    "primary sound driver",
}
_HOST_API_PRIORITY = {
    "windows wasapi": 0,
    "windows wdm-ks": 1,
    "windows directsound": 2,
    "mme": 3,
}


def display_audio_devices(
    devices: tuple[AudioDevice, ...] | None = None,
    *,
    input_only: bool = False,
    output_only: bool = False,
) -> tuple[DisplayAudioDevice, ...]:
    raw_devices = enumerate_devices() if devices is None else devices
    candidates: dict[str, tuple[tuple[int, int, int], DisplayAudioDevice]] = {}
    for order, device in enumerate(raw_devices):
        if input_only and not device.input_channels:
            continue
        if output_only and not device.output_channels:
            continue
        if _is_pseudo_device(device.name, input_only=input_only, output_only=output_only):
            continue
        label = _clean_device_label(device.name)
        if not label:
            continue
        candidate = DisplayAudioDevice(
            index=device.index,
            label=label,
            input_channels=device.input_channels,
            output_channels=device.output_channels,
            default_sample_rate=device.default_sample_rate,
        )
        key = label.casefold()
        rank = (
            _HOST_API_PRIORITY.get(device.host_api.casefold(), 10),
            0 if device.input_channels or device.output_channels else 1,
            order,
        )
        if key not in candidates or rank < candidates[key][0]:
            candidates[key] = (rank, candidate)
    return tuple(item for _, item in sorted(candidates.values(), key=lambda row: row[0]))


def _is_pseudo_device(name: str, *, input_only: bool = False, output_only: bool = False) -> bool:
    normalized = name.strip().casefold()
    pseudo_names = set()
    if input_only or not output_only:
        pseudo_names.update(_PSEUDO_INPUT_NAMES)
    if output_only or not input_only:
        pseudo_names.update(_PSEUDO_OUTPUT_NAMES)
    return normalized in pseudo_names or re.fullmatch(r"input\s*\(?\d+\)?", normalized) is not None


def _clean_device_label(name: str) -> str:
    label = " ".join(name.strip().split())
    label = _DEVICE_NAME_SUFFIX.sub("", label)
    label = label.replace("Audio Device", "").strip()
    wrapper_match = _COMMON_DEVICE_WRAPPER.match(label)
    if wrapper_match:
        label = wrapper_match.group(2).strip()
    bluetooth_match = _BLUETOOTH_INSTANCE_NAME.search(label)
    if bluetooth_match:
        device_name = bluetooth_match.group(1).strip()
        role = "Hands-Free" if _BLUETOOTH_ROLE.search(label) else ""
        label = f"{device_name} {role}".strip()
    label = _NOISY_PREFIX.sub("", label)
    label = re.sub(r"\s+", " ", label).strip()
    if _BLUETOOTH_ROLE.search(label) and "Hands-Free" not in label:
        label = _BLUETOOTH_ROLE.sub("Hands-Free", label)
    return label


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
        clap_threshold: float = 0.35,
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
        self.clap_detector = ClapDetector(threshold=clap_threshold)
        self._clap_detected = False
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

    def consume_clap_detection(self) -> bool:
        detected = self._clap_detected
        self._clap_detected = False
        return detected

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("Install the 'audio' optional dependency") from exc
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        blocksize = int(self.sample_rate * 0.03)
        pre_roll = deque(maxlen=max(1, int(self.pre_roll_seconds * self.sample_rate / blocksize)))
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
                    raw_level = self.rms(payload)
                    if self.clap_detector.observe(raw_level):
                        self._clap_detected = True
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
            raise RuntimeError(f"Unable to open or read microphone device {device}: {exc}") from exc
        finally:
            if self.diagnostic_dir and diagnostic:
                self.diagnostic_dir.mkdir(parents=True, exist_ok=True)
                name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{uuid4().hex[:8]}.wav"
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
