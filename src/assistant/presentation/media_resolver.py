from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from assistant.contracts import MediaKind, MediaSource
from assistant.presentation.media_probe import (
    MediaProbe,
    MediaProbeError,
    likely_media_path,
)
from assistant.windows.paths import KNOWN_USER_FOLDERS, USER_FOLDER_TOKENS, resolve_user_path


@dataclass(frozen=True)
class MediaResolution:
    source: MediaSource | None = None
    candidates: tuple[str, ...] = ()


class MediaResolver:
    def __init__(self, probe: MediaProbe | None = None) -> None:
        self.probe = probe or MediaProbe()

    def permission_target(
        self,
        *,
        source: str | None = None,
        root: str | None = None,
    ) -> str:
        value = source if source is not None else root
        if value is None:
            raise ValueError("A media source or search root is required")
        return str(resolve_explicit_media_path(value))

    def resolve(
        self,
        *,
        source: str | None = None,
        query: str | None = None,
        root: str | None = None,
        kind_hint: MediaKind | None = None,
        limit: int = 20,
    ) -> MediaResolution:
        if source is not None:
            return MediaResolution(
                source=self.probe.inspect(
                    resolve_explicit_media_path(source),
                    kind_hint=kind_hint,
                )
            )
        if root is None:
            raise ValueError("Media search requires a root")
        root_path = resolve_explicit_media_path(root)
        if not root_path.is_dir():
            raise MediaProbeError("The requested media search root is not a folder.")
        needle = query.casefold().strip() if query is not None else ""
        paths: list[Path] = []
        for path in root_path.rglob("*"):
            if not path.is_file() or needle and needle not in path.name.casefold():
                continue
            if not likely_media_path(path):
                continue
            try:
                candidate = self.probe.inspect(path, kind_hint=kind_hint)
            except MediaProbeError:
                continue
            paths.append(Path(candidate.location))
            if len(paths) >= limit:
                break
        if len(paths) == 1:
            return MediaResolution(source=self.probe.inspect(paths[0], kind_hint=kind_hint))
        return MediaResolution(candidates=tuple(str(path) for path in paths))


def is_explicit_media_path(value: str) -> bool:
    raw = value.strip().strip("\"'")
    if not raw:
        return False
    normalized = raw.replace("\\", "/")
    first = normalized.partition("/")[0].casefold()
    return (
        Path(raw).is_absolute()
        or raw.startswith("~")
        or first in KNOWN_USER_FOLDERS
        or any(
            raw.casefold() == token
            or raw.casefold().startswith(token + "/")
            or raw.casefold().startswith(token + "\\")
            for token in USER_FOLDER_TOKENS
        )
        or bool(re.match(r"^[a-z]:[\\/]", raw, flags=re.IGNORECASE))
        or raw.startswith("\\\\")
    )


def resolve_explicit_media_path(value: str) -> Path:
    raw = value.strip().strip("\"'")
    if not raw:
        raise ValueError("Media path cannot be blank")
    if any(character in raw for character in "*?[]"):
        raise ValueError("Media paths cannot contain wildcards")
    if not is_explicit_media_path(raw):
        raise ValueError("Use an exact path or a known user folder for media.")
    return resolve_user_path(raw)
