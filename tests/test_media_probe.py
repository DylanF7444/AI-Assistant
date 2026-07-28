from __future__ import annotations

import base64
import wave

import pytest

from assistant.contracts import MediaKind
from assistant.presentation.media_probe import (
    MediaProbe,
    MediaProbeError,
    verify_media_source,
)
from assistant.presentation.media_resolver import MediaResolver

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_probe_identifies_bounded_image_audio_and_video_metadata(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(_PNG)
    audio = tmp_path / "recording.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 80)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\0" * 32 + b"vide")

    probe = MediaProbe()
    image_source = probe.inspect(image)
    audio_source = probe.inspect(audio)
    video_source = probe.inspect(video)

    assert (image_source.kind, image_source.width, image_source.height) == (
        MediaKind.IMAGE,
        1,
        1,
    )
    assert audio_source.kind == MediaKind.AUDIO
    assert video_source.kind == MediaKind.VIDEO
    assert len(image_source.fingerprint) == 64


def test_probe_rejects_empty_malformed_wrong_kind_and_oversized_image(tmp_path):
    malformed = tmp_path / "bad.png"
    malformed.write_bytes(b"not an image")
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    image = tmp_path / "large.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (1000).to_bytes(4, "big")
        + (1000).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )

    with pytest.raises(MediaProbeError, match="supported"):
        MediaProbe().inspect(malformed)
    with pytest.raises(MediaProbeError, match="empty"):
        MediaProbe().inspect(empty)
    with pytest.raises(MediaProbeError, match="dimensions"):
        MediaProbe(max_image_pixels=100).inspect(image)
    with pytest.raises(MediaProbeError, match="not audio"):
        MediaProbe().inspect(
            image,
            kind_hint=MediaKind.AUDIO,
        )


def test_verify_media_source_detects_changes_after_authorization(tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(_PNG)
    source = MediaProbe().inspect(path)

    assert verify_media_source(source) == path.resolve()
    path.write_bytes(_PNG + b"changed")

    with pytest.raises(MediaProbeError, match="changed"):
        verify_media_source(source)


def test_media_resolver_requires_explicit_paths_and_returns_ambiguous_candidates(
    tmp_path,
):
    first = tmp_path / "holiday-one.png"
    second = tmp_path / "holiday-two.png"
    first.write_bytes(_PNG)
    second.write_bytes(_PNG)
    resolver = MediaResolver()

    with pytest.raises(ValueError, match="exact path"):
        resolver.resolve(source="photo.png")
    resolution = resolver.resolve(query="holiday", root=str(tmp_path))

    assert resolution.source is None
    assert resolution.candidates == (str(first.resolve()), str(second.resolve()))
