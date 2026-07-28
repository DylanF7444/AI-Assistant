from __future__ import annotations

import hashlib
import mimetypes
import struct
from dataclasses import dataclass
from pathlib import Path

from assistant.contracts import MediaKind, MediaSource, MediaSourceType

_PROBE_BYTES = 1024 * 1024
_FINGERPRINT_BYTES = 64 * 1024
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"})
_AUDIO_EXTENSIONS = frozenset(
    {".wav", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".aac", ".m4a", ".m4b"}
)
_VIDEO_EXTENSIONS = frozenset({".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm"})
SUPPORTED_MEDIA_EXTENSIONS = _IMAGE_EXTENSIONS | _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS


class MediaProbeError(ValueError):
    """Safe validation failure for a media source."""


@dataclass(frozen=True)
class _DetectedMedia:
    kind: MediaKind
    mime_type: str
    width: int | None = None
    height: int | None = None


class MediaProbe:
    """Inspect bounded file metadata without decoding full media content."""

    def __init__(
        self,
        *,
        max_probe_bytes: int = _PROBE_BYTES,
        max_image_pixels: int = 100_000_000,
    ) -> None:
        if max_probe_bytes < 64:
            raise ValueError("Media probe window is too small")
        if max_image_pixels < 1:
            raise ValueError("Maximum image pixels must be positive")
        self.max_probe_bytes = max_probe_bytes
        self.max_image_pixels = max_image_pixels

    def inspect(
        self,
        path: str | Path,
        *,
        source_type: MediaSourceType = MediaSourceType.LOCAL_FILE,
        origin: str | None = None,
        kind_hint: MediaKind | None = None,
    ) -> MediaSource:
        resolved = _canonical_media_file(path)
        stat = resolved.stat()
        if stat.st_size <= 0:
            raise MediaProbeError("The media file is empty.")
        with resolved.open("rb") as handle:
            header = handle.read(self.max_probe_bytes)
        detected = _detect_media(resolved, header)
        if kind_hint is not None and detected.kind != kind_hint:
            raise MediaProbeError(
                f"The file is {detected.kind.value} media, not {kind_hint.value} media."
            )
        if (
            detected.width is not None
            and detected.height is not None
            and detected.width * detected.height > self.max_image_pixels
        ):
            raise MediaProbeError("The image dimensions exceed the configured safety limit.")
        return MediaSource(
            source_type=source_type,
            location=str(resolved),
            origin=origin,
            kind=detected.kind,
            mime_type=detected.mime_type,
            title=resolved.name,
            size_bytes=stat.st_size,
            fingerprint=media_file_fingerprint(resolved),
            width=detected.width,
            height=detected.height,
        )


def media_file_fingerprint(path: str | Path) -> str:
    """Return a bounded content and identity fingerprint for change detection."""

    resolved = _canonical_media_file(path)
    stat = resolved.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode("ascii"))
    digest.update(b":")
    digest.update(str(stat.st_mtime_ns).encode("ascii"))
    digest.update(b":")
    digest.update(str(getattr(stat, "st_ino", 0)).encode("ascii"))
    with resolved.open("rb") as handle:
        digest.update(handle.read(_FINGERPRINT_BYTES))
        if stat.st_size > _FINGERPRINT_BYTES:
            handle.seek(max(0, stat.st_size - _FINGERPRINT_BYTES))
            digest.update(handle.read(_FINGERPRINT_BYTES))
    return digest.hexdigest()


def verify_media_source(source: MediaSource) -> Path:
    """Revalidate an authorized source immediately before presentation."""

    path = _canonical_media_file(source.location)
    stat = path.stat()
    if stat.st_size != source.size_bytes:
        raise MediaProbeError("The media file changed after it was authorized.")
    if media_file_fingerprint(path) != source.fingerprint:
        raise MediaProbeError("The media file changed after it was authorized.")
    return path


def _canonical_media_file(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise MediaProbeError("The requested media file does not exist.") from exc
    except OSError as exc:
        raise MediaProbeError("The requested media path could not be inspected.") from exc
    if not resolved.is_file():
        raise MediaProbeError("The requested media source is not a file.")
    return resolved


def _detect_media(path: Path, header: bytes) -> _DetectedMedia:
    suffix = path.suffix.casefold()
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = _png_dimensions(header)
        return _DetectedMedia(MediaKind.IMAGE, "image/png", width, height)
    if header.startswith(b"\xff\xd8\xff"):
        width, height = _jpeg_dimensions(header)
        return _DetectedMedia(MediaKind.IMAGE, "image/jpeg", width, height)
    if header.startswith((b"GIF87a", b"GIF89a")):
        width, height = struct.unpack("<HH", header[6:10])
        return _DetectedMedia(MediaKind.IMAGE, "image/gif", width, height)
    if header.startswith(b"BM"):
        width, height = _bmp_dimensions(header)
        return _DetectedMedia(MediaKind.IMAGE, "image/bmp", width, height)
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        width, height = _webp_dimensions(header)
        return _DetectedMedia(MediaKind.IMAGE, "image/webp", width, height)
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return _DetectedMedia(MediaKind.AUDIO, "audio/wav")
    if header.startswith(b"fLaC"):
        return _DetectedMedia(MediaKind.AUDIO, "audio/flac")
    if _looks_like_aac(header):
        return _DetectedMedia(MediaKind.AUDIO, "audio/aac")
    if header.startswith(b"ID3") or _looks_like_mp3_frame(header):
        return _DetectedMedia(MediaKind.AUDIO, "audio/mpeg")
    if header.startswith(b"OggS"):
        if b"theora" in header[:4096].lower():
            return _DetectedMedia(MediaKind.VIDEO, "video/ogg")
        return _DetectedMedia(MediaKind.AUDIO, "audio/ogg")
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return _DetectedMedia(MediaKind.VIDEO, "video/x-msvideo")
    if header.startswith(b"\x1aE\xdf\xa3"):
        if suffix in {".webm"}:
            return _DetectedMedia(MediaKind.VIDEO, "video/webm")
        return _DetectedMedia(MediaKind.VIDEO, "video/x-matroska")
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return _detect_iso_media(path, header)
    raise MediaProbeError("The file is not a supported image, audio, or video format.")


def _detect_iso_media(path: Path, header: bytes) -> _DetectedMedia:
    suffix = path.suffix.casefold()
    folded = header.lower()
    if suffix in {".m4a", ".m4b"} or (b"soun" in folded and b"vide" not in folded):
        return _DetectedMedia(MediaKind.AUDIO, "audio/mp4")
    mime_type = "video/quicktime" if suffix == ".mov" else "video/mp4"
    return _DetectedMedia(MediaKind.VIDEO, mime_type)


def _png_dimensions(header: bytes) -> tuple[int, int]:
    if len(header) < 24 or header[12:16] != b"IHDR":
        raise MediaProbeError("The PNG header is malformed.")
    return struct.unpack(">II", header[16:24])


def _jpeg_dimensions(header: bytes) -> tuple[int | None, int | None]:
    offset = 2
    while offset + 9 <= len(header):
        if header[offset] != 0xFF:
            offset += 1
            continue
        marker = header[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(header):
            break
        length = int.from_bytes(header[offset : offset + 2], "big")
        if length < 2 or offset + length > len(header):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = int.from_bytes(header[offset + 3 : offset + 5], "big")
            width = int.from_bytes(header[offset + 5 : offset + 7], "big")
            return width, height
        offset += length
    return None, None


def _bmp_dimensions(header: bytes) -> tuple[int | None, int | None]:
    if len(header) < 26:
        raise MediaProbeError("The BMP header is malformed.")
    dib_size = int.from_bytes(header[14:18], "little")
    if dib_size == 12 and len(header) >= 26:
        return struct.unpack("<HH", header[18:22])
    if dib_size >= 40 and len(header) >= 26:
        width, height = struct.unpack("<ii", header[18:26])
        return abs(width), abs(height)
    return None, None


def _webp_dimensions(header: bytes) -> tuple[int | None, int | None]:
    if len(header) < 30:
        return None, None
    chunk = header[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(header[24:27], "little")
        height = 1 + int.from_bytes(header[27:30], "little")
        return width, height
    return None, None


def _looks_like_mp3_frame(header: bytes) -> bool:
    return len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0


def _looks_like_aac(header: bytes) -> bool:
    return len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0


def likely_media_path(path: Path) -> bool:
    suffix = path.suffix.casefold()
    if suffix in SUPPORTED_MEDIA_EXTENSIONS:
        return True
    mime_type, _ = mimetypes.guess_type(path.name)
    return bool(mime_type and mime_type.partition("/")[0] in {"image", "audio", "video"})
