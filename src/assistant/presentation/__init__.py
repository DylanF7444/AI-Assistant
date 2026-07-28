"""Typed, UI-independent media presentation services."""

from assistant.presentation.media_probe import (
    MediaProbe,
    MediaProbeError,
    media_file_fingerprint,
    verify_media_source,
)
from assistant.presentation.media_resolver import (
    MediaResolution,
    MediaResolver,
)
from assistant.presentation.session import (
    MediaSessionRegistry,
    MediaSessionSnapshot,
    MediaSessionStatus,
)

__all__ = [
    "MediaProbe",
    "MediaProbeError",
    "MediaResolution",
    "MediaResolver",
    "MediaSessionRegistry",
    "MediaSessionSnapshot",
    "MediaSessionStatus",
    "media_file_fingerprint",
    "verify_media_source",
]
