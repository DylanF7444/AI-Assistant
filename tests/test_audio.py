import struct
import sys

import pytest

from assistant.audio.capture import AudioCapture


def test_audio_rms_detects_silence_and_signal():
    assert AudioCapture.rms(b"\x00\x00" * 100) == 0
    signal = struct.pack("<h", 16384) * 100
    assert 0.49 < AudioCapture.rms(signal) < 0.51


def test_noise_suppression_and_gain_control():
    capture = AudioCapture(vad_threshold=0.02)
    quiet = struct.pack("<h", 100) * 100
    assert capture.process(quiet) == b"\x00" * len(quiet)
    medium = struct.pack("<h", 3000) * 100
    assert AudioCapture.rms(capture.process(medium)) > AudioCapture.rms(medium)


@pytest.mark.asyncio
async def test_unavailable_microphone_has_actionable_error(monkeypatch):
    class BrokenSoundDevice:
        @staticmethod
        def RawInputStream(**kwargs):
            raise OSError("device unavailable")

    monkeypatch.setitem(sys.modules, "sounddevice", BrokenSoundDevice)
    stream = AudioCapture(device=7).chunks()
    with pytest.raises(RuntimeError, match="microphone device 7.*device unavailable"):
        await anext(stream)
