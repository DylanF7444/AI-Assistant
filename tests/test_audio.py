import struct
import sys

import pytest

from assistant.audio.capture import AudioCapture, AudioDevice, display_audio_devices


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


def test_display_audio_devices_cleans_windows_input_names():
    devices = (
        AudioDevice(0, "Microsoft Sound Mapper - Input", 2, 0, 44100, "MME"),
        AudioDevice(1, "Microphone (TONOR TC30 Audio Device)", 2, 0, 44100, "MME"),
        AudioDevice(2, "Headset (soundcore Q20)", 1, 0, 44100, "MME"),
        AudioDevice(3, "Primary Sound Capture Driver", 2, 0, 44100, "Windows DirectSound"),
        AudioDevice(4, "Microphone (TONOR TC30 Audio Device)", 2, 0, 44100, "Windows WASAPI"),
        AudioDevice(5, "Headset (soundcore Q20)", 1, 0, 44100, "Windows WASAPI"),
        AudioDevice(
            6,
            "Headset (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-Free%0 ;(soundcore Q20))",
            1,
            0,
            16000,
            "Windows WDM-KS",
        ),
        AudioDevice(7, "Input 0", 1, 0, 44100, "Windows WDM-KS"),
        AudioDevice(
            8,
            "Input (@System32\\drivers\\bthhfenum.sys,#4;%1 Hands-Free HF Audio%0 ;(Galaxy S8))",
            1,
            0,
            16000,
            "Windows WDM-KS",
        ),
        AudioDevice(9, "Microphone (USB CAMERA) (2- in)", 1, 0, 48000, "Windows WDM-KS"),
        AudioDevice(10, "Speakers (Realtek Audio)", 0, 2, 48000, "Windows WASAPI"),
    )

    labels = [device.label for device in display_audio_devices(devices, input_only=True)]

    assert labels == [
        "TONOR TC30",
        "soundcore Q20",
        "soundcore Q20 Hands-Free",
        "Galaxy S8 Hands-Free",
        "USB CAMERA",
    ]


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
