from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from assistant.contracts import AudioSession, Transcript
from assistant.stt.faster_whisper import FasterWhisperProvider


class FakeStreamingWhisper(FasterWhisperProvider):
    async def _load(self):
        self._model = object()

    async def _decode(self, session, chunks):
        return Transcript(
            session_id=session.session_id,
            raw_text=f"{len(chunks)} bytes",
            normalized_text=f"{len(chunks)} bytes",
            confidence=0.9,
            is_final=True,
        )


@pytest.mark.asyncio
async def test_whisper_emits_partial_and_final_transcripts():
    provider = FakeStreamingWhisper(
        partial_interval_seconds=0.01,
        partial_min_audio_seconds=0.01,
    )
    session = AudioSession(source="test", sample_rate=16000)

    async def audio():
        for _ in range(4):
            yield b"\x01\x00" * 400
            await asyncio.sleep(0.015)

    values = [item async for item in provider.transcribe(session, audio())]
    assert any(not item.is_final for item in values)
    assert values[-1].is_final
    assert values[-1].raw_text.endswith("bytes")


@pytest.mark.asyncio
async def test_health_reports_missing_stt_dependency(monkeypatch):
    provider = FasterWhisperProvider("small.en")
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "faster_whisper" else real_find_spec(name),
    )
    health = await provider.health()
    assert not health["healthy"]
    assert "not installed" in health["error"]


@pytest.mark.asyncio
async def test_health_reports_missing_local_model(monkeypatch, tmp_path):
    provider = FasterWhisperProvider("missing-model")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    health = await provider.health()
    assert not health["healthy"]
    assert "local STT model" in health["error"]


@pytest.mark.asyncio
async def test_explicit_cpu_uses_int8_compute_type(monkeypatch, tmp_path):
    calls = []

    class FakeWhisperModel:
        def __init__(self, model, *, device, compute_type):
            calls.append((model, device, compute_type))

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    provider = FasterWhisperProvider(str(tmp_path), "cpu")
    monkeypatch.setattr(
        provider,
        "health",
        lambda: _async_value({"healthy": True}),
    )
    await provider._load()
    assert calls == [(str(tmp_path), "cpu", "int8")]


@pytest.mark.asyncio
async def test_auto_falls_back_to_cpu_when_cuda_runtime_missing(monkeypatch, tmp_path):
    calls = []

    class FakeWhisperModel:
        def __init__(self, model, *, device, compute_type):
            calls.append((model, device, compute_type))

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    provider = FasterWhisperProvider(str(tmp_path), "auto")
    monkeypatch.setattr(provider, "health", lambda: _async_value({"healthy": True}))
    monkeypatch.setattr(provider, "_cuda_runtime_available", lambda: False)

    await provider._load()

    assert calls == [(str(tmp_path), "cpu", "int8")]


@pytest.mark.asyncio
async def test_explicit_cuda_reports_missing_runtime(monkeypatch, tmp_path):
    class FakeWhisperModel:
        def __init__(self, model, *, device, compute_type):
            raise AssertionError("CUDA should be rejected before model load")

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    provider = FasterWhisperProvider(str(tmp_path), "cuda")
    monkeypatch.setattr(provider, "health", lambda: _async_value({"healthy": True}))
    monkeypatch.setattr(provider, "_cuda_runtime_available", lambda: False)

    with pytest.raises(RuntimeError, match="cublas64_12.dll"):
        await provider._load()


async def _async_value(value):
    return value
