from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import mimetypes
import os
import shutil
import socket
import time
from collections.abc import Callable, Iterable
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from assistant.contracts import MediaSource, MediaSourceType
from assistant.presentation.media_probe import MediaProbe, MediaProbeError

DEFAULT_MEDIA_DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_MEDIA_CACHE_MAX_BYTES = 500 * 1024 * 1024
DEFAULT_MEDIA_CACHE_RETENTION_DAYS = 7
_CHUNK_BYTES = 64 * 1024
_PROGRESS_BYTES = 1024 * 1024
_USER_AGENT = "LocalAssistant/0.1 MediaCache"

AddressResolver = Callable[..., Iterable[tuple]]
ProgressCallback = Callable[[int, int | None], None]


class RemoteMediaError(ValueError):
    """Safe failure while validating or acquiring explicit remote media."""


def is_explicit_remote_media_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme.casefold() == "https" and bool(parsed.hostname)


def sanitized_media_url(value: str) -> str:
    parsed = urlparse(value.strip())
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunparse(("https", host, parsed.path or "/", "", "", ""))


def validate_public_https_url(
    value: str,
    *,
    address_resolver: AddressResolver = socket.getaddrinfo,
) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RemoteMediaError(
            "Remote media requires a public HTTPS URL without embedded credentials."
        )
    try:
        port = parsed.port or 443
    except ValueError as error:
        raise RemoteMediaError("The remote media URL has an invalid port.") from error
    try:
        addresses = tuple(address_resolver(parsed.hostname, port))
    except (OSError, socket.gaierror) as error:
        raise RemoteMediaError(
            f"Could not resolve the remote media host: {parsed.hostname}"
        ) from error
    if not addresses:
        raise RemoteMediaError("The remote media host did not resolve to an address.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        except (IndexError, ValueError) as error:
            raise RemoteMediaError("The remote media host returned an invalid address.") from error
        if not ip.is_global:
            raise RemoteMediaError("Private, local, and reserved remote media targets are blocked.")
    return value.strip()


class _SafeMediaRedirectHandler(HTTPRedirectHandler):
    def __init__(self, address_resolver: AddressResolver) -> None:
        super().__init__()
        self.address_resolver = address_resolver

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_https_url(
            newurl,
            address_resolver=self.address_resolver,
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class RemoteMediaCache:
    """Cancellable, bounded, content-addressed cache for explicit media URLs."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        offline_lock: bool = True,
        max_download_bytes: int = DEFAULT_MEDIA_DOWNLOAD_MAX_BYTES,
        max_cache_bytes: int = DEFAULT_MEDIA_CACHE_MAX_BYTES,
        retention_days: int = DEFAULT_MEDIA_CACHE_RETENTION_DAYS,
        timeout_seconds: float = 30,
        opener=None,
        address_resolver: AddressResolver = socket.getaddrinfo,
        probe: MediaProbe | None = None,
    ) -> None:
        if max_download_bytes < 1 or max_cache_bytes < max_download_bytes:
            raise ValueError("Media cache limits are inconsistent")
        if retention_days < 1 or timeout_seconds <= 0:
            raise ValueError("Media cache retention and timeout must be positive")
        self.cache_dir = Path(cache_dir).resolve()
        self.offline_lock = bool(offline_lock)
        self.max_download_bytes = max_download_bytes
        self.max_cache_bytes = max_cache_bytes
        self.retention_days = retention_days
        self.timeout_seconds = timeout_seconds
        self._opener = opener
        self._address_resolver = address_resolver
        self.probe = probe or MediaProbe()

    def set_offline_lock(self, enabled: bool) -> None:
        self.offline_lock = bool(enabled)

    def permission_target(self, url: str) -> str:
        if self.offline_lock:
            raise RemoteMediaError("Remote media downloads are blocked by the global offline lock.")
        validated = validate_public_https_url(
            url,
            address_resolver=self._address_resolver,
        )
        return sanitized_media_url(validated)

    async def acquire(
        self,
        url: str,
        *,
        cancel_event: asyncio.Event,
        progress: ProgressCallback | None = None,
    ) -> MediaSource:
        if self.offline_lock:
            raise RemoteMediaError("Remote media downloads are blocked by the global offline lock.")
        return await asyncio.to_thread(
            self._download,
            url,
            cancel_event,
            progress,
        )

    def _download(
        self,
        url: str,
        cancel_event: asyncio.Event,
        progress: ProgressCallback | None,
    ) -> MediaSource:
        validated = validate_public_https_url(
            url,
            address_resolver=self._address_resolver,
        )
        if cancel_event.is_set():
            raise asyncio.CancelledError
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.prune()
        temporary = self.cache_dir / f".{uuid4().hex}.download"
        destination: Path | None = None
        keep_destination = False
        request = Request(
            validated,
            headers={
                "Accept": "image/*, audio/*, video/*",
                "User-Agent": _USER_AGENT,
            },
        )
        opener = self._opener or build_opener(_SafeMediaRedirectHandler(self._address_resolver))
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                final_url = response.geturl()
                validate_public_https_url(
                    final_url,
                    address_resolver=self._address_resolver,
                )
                content_type = _content_type(response.headers)
                if content_type.partition("/")[0] not in {"image", "audio", "video"}:
                    raise RemoteMediaError(
                        "The remote server did not return image, audio, or video content."
                    )
                total = _content_length(response.headers)
                if total is not None and total > self.max_download_bytes:
                    raise RemoteMediaError(
                        "The remote media exceeds the configured download limit."
                    )
                digest = hashlib.sha256()
                downloaded = 0
                last_reported = 0
                with temporary.open("xb") as output:
                    while True:
                        if cancel_event.is_set():
                            raise asyncio.CancelledError
                        chunk = response.read(_CHUNK_BYTES)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > self.max_download_bytes:
                            raise RemoteMediaError(
                                "The remote media exceeds the configured download limit."
                            )
                        digest.update(chunk)
                        output.write(chunk)
                        if progress is not None and (
                            downloaded - last_reported >= _PROGRESS_BYTES
                            or total is not None
                            and downloaded >= total
                        ):
                            progress(downloaded, total)
                            last_reported = downloaded
                if downloaded == 0:
                    raise RemoteMediaError("The remote server returned an empty media file.")
                extension = _safe_extension(final_url, content_type)
                destination = self.cache_dir / f"{digest.hexdigest()}{extension}"
                os.replace(temporary, destination)
                source = self.probe.inspect(
                    destination,
                    source_type=MediaSourceType.REMOTE_CACHE,
                    origin=sanitized_media_url(final_url),
                )
                if source.mime_type.partition("/")[0] != content_type.partition("/")[0]:
                    destination.unlink(missing_ok=True)
                    raise RemoteMediaError(
                        "The remote media content does not match its declared type."
                    )
                title = _safe_title(final_url, extension)
                source = source.model_copy(
                    update={
                        "title": title,
                    }
                )
                self.prune(protected=destination)
                keep_destination = True
                return source
        except asyncio.CancelledError:
            raise
        except HTTPError as error:
            raise RemoteMediaError(
                f"The remote media server returned HTTP {error.code}."
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise RemoteMediaError("The remote media download failed.") from error
        except MediaProbeError as error:
            raise RemoteMediaError(
                "The downloaded file is not supported image, audio, or video media."
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
            if destination is not None and not keep_destination:
                destination.unlink(missing_ok=True)

    def cache_size(self) -> int:
        if not self.cache_dir.exists():
            return 0
        return sum(path.stat().st_size for path in self.cache_dir.iterdir() if path.is_file())

    def prune(self, *, protected: Path | None = None) -> int:
        if not self.cache_dir.exists():
            return 0
        removed = 0
        cutoff = time.time() - self.retention_days * 86400
        files = sorted(
            (path for path in self.cache_dir.iterdir() if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
        for path in tuple(files):
            if path == protected:
                continue
            stat = path.stat()
            if path.name.startswith(".") or stat.st_mtime < cutoff:
                removed += stat.st_size
                path.unlink(missing_ok=True)
                files.remove(path)
        total = sum(path.stat().st_size for path in files if path.exists())
        for path in files:
            if total <= self.max_cache_bytes:
                break
            if path == protected or not path.exists():
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            removed += size
        return removed

    def clear(self) -> int:
        if not self.cache_dir.exists():
            return 0
        removed = self.cache_size()
        for child in tuple(self.cache_dir.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        return removed


def _content_type(headers: Message) -> str:
    if hasattr(headers, "get_content_type"):
        return str(headers.get_content_type()).casefold()
    return str(headers.get("Content-Type", "")).partition(";")[0].strip().casefold()


def _content_length(headers: Message) -> int | None:
    value = headers.get("Content-Length")
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError) as error:
        raise RemoteMediaError("The remote media size header is invalid.") from error
    if length < 0:
        raise RemoteMediaError("The remote media size header is invalid.")
    return length


def _safe_extension(url: str, content_type: str) -> str:
    suffix = Path(unquote(urlparse(url).path)).suffix.casefold()
    allowed = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".wav",
        ".mp3",
        ".flac",
        ".ogg",
        ".oga",
        ".opus",
        ".aac",
        ".m4a",
        ".m4b",
        ".mp4",
        ".m4v",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
    }
    if suffix in allowed:
        return suffix
    guessed = (mimetypes.guess_extension(content_type) or "").casefold()
    aliases = {".jpe": ".jpg", ".qt": ".mov", ".wave": ".wav"}
    return aliases.get(guessed, guessed) if guessed in allowed or guessed in aliases else ".media"


def _safe_title(url: str, extension: str) -> str:
    name = Path(unquote(urlparse(url).path)).name.strip()
    if not name or any(ord(character) < 32 for character in name):
        name = f"downloaded-media{extension}"
    return name[:500]
