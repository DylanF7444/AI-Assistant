from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import pytest

from assistant.contracts import PipelineState, Response
from assistant.tts.discovery import (
    find_piper_executable,
    find_piper_voice_models,
    piper_model_dialog_directory,
)
from assistant.tts.elevenlabs import ElevenLabsProvider


class _AudioResponse:
    def __init__(self, audio: bytes) -> None:
        self._stream = io.BytesIO(audio)

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class _RecordingOpener:
    def __init__(self, audio: bytes) -> None:
        self.audio = audio
        self.requests = []

    def open(self, request, *, timeout: float):
        self.requests.append((request, timeout))
        return _AudioResponse(self.audio)


@pytest.mark.asyncio
async def test_elevenlabs_requests_selected_voice_and_model_and_plays_wave(tmp_path):
    opener = _RecordingOpener(b"\x00\x00\x10\x00")
    provider = ElevenLabsProvider(
        "secret-key",
        "voice_123",
        "eleven_flash_v2_5",
        cache_dir=tmp_path / "cache",
        opener=opener,
    )
    played = {}

    def inspect_wave(path: Path) -> None:
        with wave.open(str(path), "rb") as audio:
            played.update(
                channels=audio.getnchannels(),
                sample_width=audio.getsampwidth(),
                sample_rate=audio.getframerate(),
                frames=audio.readframes(audio.getnframes()),
            )

    provider._play = inspect_wave

    events = [
        event async for event in provider.speak(Response(display_text="Hello", speech_text="Hello"))
    ]

    assert [event.state for event in events] == [PipelineState.SPEAKING]
    assert played == {
        "channels": 1,
        "sample_width": 2,
        "sample_rate": 24_000,
        "frames": b"\x00\x00\x10\x00",
    }
    request, timeout = opener.requests[0]
    assert request.full_url.endswith("/text-to-speech/voice_123?output_format=pcm_24000")
    assert json.loads(request.data) == {
        "text": "Hello",
        "model_id": "eleven_flash_v2_5",
    }
    assert request.get_header("Xi-api-key") == "secret-key"
    assert timeout == 30.0


@pytest.mark.asyncio
async def test_elevenlabs_reuses_short_speech_cache_without_a_second_request(tmp_path):
    opener = _RecordingOpener(b"\x00\x00")
    provider = ElevenLabsProvider(
        "secret-key",
        "voice",
        cache_dir=tmp_path / "cache",
        opener=opener,
    )
    provider._play = lambda _path: None
    response = Response(display_text="Cached", speech_text="Cached")

    for _ in range(2):
        async for _event in provider.speak(response):
            pass

    assert len(opener.requests) == 1


@pytest.mark.asyncio
async def test_elevenlabs_offline_lock_blocks_network_and_health_redacts_key(tmp_path):
    opener = _RecordingOpener(b"\x00\x00")
    provider = ElevenLabsProvider(
        "never-print-this-key",
        "voice",
        offline_lock=True,
        opener=opener,
    )

    with pytest.raises(RuntimeError, match="offline lock"):
        async for _event in provider.speak(Response(display_text="Blocked", speech_text="Blocked")):
            pass

    health = await provider.health()
    assert health["healthy"] is False
    assert health["api_key_configured"] is True
    assert "never-print-this-key" not in str(health)
    assert opener.requests == []


def test_piper_discovery_finds_paired_voice_files_in_common_model_roots(tmp_path):
    data_dir = tmp_path / "data"
    working = tmp_path / "project"
    voice_dir = working / "models" / "piper"
    voice_dir.mkdir(parents=True)
    voice = voice_dir / "en_US-test-medium.onnx"
    voice.write_bytes(b"voice")
    voice.with_suffix(".onnx.json").write_text("{}", encoding="utf-8")
    (voice_dir / "not-a-piper-model.onnx").write_bytes(b"other")

    found = find_piper_voice_models(
        data_dir,
        working_directory=working,
        executable_directory=tmp_path / "application",
    )

    assert found == (voice.resolve(),)
    assert (
        piper_model_dialog_directory(
            data_dir,
            working_directory=working,
            executable_directory=tmp_path / "application",
        )
        == voice_dir.resolve()
    )


def test_piper_discovery_finds_bundled_executable(tmp_path):
    data_dir = tmp_path / "data"
    executable = data_dir / "piper" / "piper.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"executable")

    assert (
        find_piper_executable(
            data_dir,
            working_directory=tmp_path / "project",
            executable_directory=tmp_path / "application",
        )
        == executable.resolve()
    )
