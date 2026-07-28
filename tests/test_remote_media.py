from __future__ import annotations

import asyncio
import base64
import io
import os
import time
from email.message import Message

import pytest

from assistant.bootstrap import build_container
from assistant.contracts import (
    MediaKind,
    MediaSourceType,
    PermissionContext,
    PipelineState,
    ResultStatus,
    SkillCall,
)
from assistant.events import EventBus
from assistant.policy.permissions import GrantType
from assistant.presentation.media_resolver import MediaResolver
from assistant.presentation.remote_media import (
    RemoteMediaCache,
    RemoteMediaError,
    sanitized_media_url,
    validate_public_https_url,
)
from assistant.presentation.session import MediaSessionRegistry
from assistant.skills.media import MediaPresentSkill

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def public_resolver(host: str, port: int):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        content_type: str = "image/png",
        final_url: str = "https://cdn.example/media/photo.png",
        content_length: int | None = None,
    ) -> None:
        self._stream = io.BytesIO(payload)
        self._final_url = final_url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(
            len(payload) if content_length is None else content_length
        )

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def geturl(self) -> str:
        return self._final_url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests = []

    def open(self, request, *, timeout: float):
        self.requests.append((request, timeout))
        return self.response


def remote_cache(tmp_path, response: FakeResponse, **overrides) -> RemoteMediaCache:
    return RemoteMediaCache(
        tmp_path / "media-cache",
        offline_lock=False,
        opener=FakeOpener(response),
        address_resolver=public_resolver,
        **overrides,
    )


@pytest.mark.asyncio
async def test_remote_media_downloads_to_verified_cache_with_sanitized_provenance(
    tmp_path,
):
    url = "https://images.example/photo.png?access_token=never-store-this#fragment"
    opener = FakeOpener(
        FakeResponse(
            _PNG,
            final_url="https://cdn.example/assets/photo.png?temporary=secret",
        )
    )
    cache = RemoteMediaCache(
        tmp_path / "media-cache",
        offline_lock=False,
        opener=opener,
        address_resolver=public_resolver,
    )
    progress = []

    source = await cache.acquire(
        url,
        cancel_event=asyncio.Event(),
        progress=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert source.source_type == MediaSourceType.REMOTE_CACHE
    assert source.kind == MediaKind.IMAGE
    assert source.title == "photo.png"
    assert source.origin == "https://cdn.example/assets/photo.png"
    assert "secret" not in source.model_dump_json()
    assert source.location.startswith(str(cache.cache_dir))
    assert cache.cache_size() == len(_PNG)
    assert opener.requests[0][0].get_header("Accept") == "image/*, audio/*, video/*"
    assert progress == [(len(_PNG), len(_PNG))]


@pytest.mark.parametrize(
    "url",
    (
        "http://example.com/photo.png",
        "https://user:password@example.com/photo.png",
        "https://example.com:bad/photo.png",
    ),
)
def test_remote_media_rejects_unsafe_url_shapes(url):
    with pytest.raises(RemoteMediaError):
        validate_public_https_url(url, address_resolver=public_resolver)


def test_remote_media_rejects_private_and_redirected_private_destinations(tmp_path):
    def resolver(host: str, port: int):
        address = "127.0.0.1" if host in {"localhost", "internal.example"} else "93.184.216.34"
        return [(2, 1, 6, "", (address, port))]

    with pytest.raises(RemoteMediaError, match="Private"):
        validate_public_https_url(
            "https://localhost/photo.png",
            address_resolver=resolver,
        )

    cache = RemoteMediaCache(
        tmp_path / "media-cache",
        offline_lock=False,
        opener=FakeOpener(
            FakeResponse(
                _PNG,
                final_url="https://internal.example/photo.png",
            )
        ),
        address_resolver=resolver,
    )
    with pytest.raises(RemoteMediaError, match="Private"):
        cache._download(
            "https://public.example/photo.png",
            asyncio.Event(),
            None,
        )
    assert cache.cache_size() == 0


@pytest.mark.asyncio
async def test_remote_media_enforces_offline_size_mime_and_cancellation(tmp_path):
    offline = RemoteMediaCache(
        tmp_path / "offline",
        offline_lock=True,
        address_resolver=public_resolver,
    )
    with pytest.raises(RemoteMediaError, match="offline lock"):
        await offline.acquire(
            "https://example.com/photo.png",
            cancel_event=asyncio.Event(),
        )

    oversized = remote_cache(
        tmp_path,
        FakeResponse(_PNG, content_length=10_000),
        max_download_bytes=100,
        max_cache_bytes=200,
    )
    with pytest.raises(RemoteMediaError, match="download limit"):
        await oversized.acquire(
            "https://example.com/photo.png",
            cancel_event=asyncio.Event(),
        )

    wrong_mime = remote_cache(
        tmp_path,
        FakeResponse(_PNG, content_type="text/html"),
    )
    with pytest.raises(RemoteMediaError, match="did not return"):
        await wrong_mime.acquire(
            "https://example.com/photo.png",
            cancel_event=asyncio.Event(),
        )

    mismatched = remote_cache(
        tmp_path,
        FakeResponse(_PNG, content_type="audio/wav"),
    )
    with pytest.raises(RemoteMediaError, match="does not match"):
        await mismatched.acquire(
            "https://example.com/photo.png",
            cancel_event=asyncio.Event(),
        )
    assert mismatched.cache_size() == 0

    cancelled = asyncio.Event()
    cancelled.set()
    cancellable = remote_cache(tmp_path, FakeResponse(_PNG))
    with pytest.raises(asyncio.CancelledError):
        await cancellable.acquire(
            "https://example.com/photo.png",
            cancel_event=cancelled,
        )
    assert not any(path.name.startswith(".") for path in cancellable.cache_dir.glob("*"))


def test_remote_media_cache_prunes_by_retention_and_size_and_clears(tmp_path):
    cache = RemoteMediaCache(
        tmp_path / "media-cache",
        offline_lock=False,
        max_download_bytes=100,
        max_cache_bytes=100,
        retention_days=1,
        address_resolver=public_resolver,
    )
    cache.cache_dir.mkdir(parents=True)
    old = cache.cache_dir / "old.bin"
    first = cache.cache_dir / "first.bin"
    second = cache.cache_dir / "second.bin"
    old.write_bytes(b"x" * 10)
    first.write_bytes(b"x" * 60)
    second.write_bytes(b"x" * 60)
    os.utime(old, (time.time() - 172800, time.time() - 172800))
    os.utime(first, (time.time() - 20, time.time() - 20))

    removed = cache.prune()

    assert removed == 70
    assert not old.exists()
    assert not first.exists()
    assert second.exists()
    assert cache.clear() == 60
    assert cache.cache_size() == 0


@pytest.mark.asyncio
async def test_remote_media_skill_uses_network_permission_and_reports_progress(
    tmp_path,
):
    url = "https://example.com/photo.png?signature=private"
    events = EventBus()
    cache = remote_cache(tmp_path, FakeResponse(_PNG))
    sessions = MediaSessionRegistry()
    skill = MediaPresentSkill(MediaResolver(), sessions, cache, events)
    call = SkillCall(
        skill_id="media.present",
        arguments={"source": url},
        permission_context=PermissionContext(session_id="remote-media"),
    )

    scopes = skill.required_permissions(call)
    result = await skill.execute(call, asyncio.Event())

    assert scopes[0].capability == "network"
    assert scopes[0].targets == ("https://example.com/photo.png",)
    assert result.status == ResultStatus.COMPLETED
    assert result.media_directive.source.source_type == MediaSourceType.REMOTE_CACHE
    progress = [
        event.progress
        for event in events.history(call.correlation_id)
        if event.state == PipelineState.EXECUTING
    ]
    assert progress[0] == 0
    assert progress[-1] == 1
    assert sanitized_media_url(url) == "https://example.com/photo.png"


@pytest.mark.asyncio
async def test_remote_media_routes_through_permission_engine_without_logging_query(
    settings,
    capsys,
):
    url = "https://example.com/photo.png?signature=never-log-this"
    configured = settings.model_copy(update={"offline_lock": False, "terminal_debug": True})
    container = build_container(configured)
    container.media_cache._opener = FakeOpener(FakeResponse(_PNG))
    container.media_cache._address_resolver = public_resolver

    permission_response, permission_outcome = await container.orchestrator.handle_text(
        f"show {url}"
    )

    assert permission_response.response_type.value == "permission"
    scope = permission_outcome.permission_requests[0].scope
    assert scope.capability == "network"
    assert scope.targets == ("https://example.com/photo.png",)
    container.permissions.grant(
        user_id="local-user",
        skill_id="media.present",
        capability="network",
        grant_type=GrantType.SESSION,
        targets=scope.targets,
        session_id="desktop",
    )

    response, outcome = await container.orchestrator.handle_text(f"show {url}")

    assert outcome.result.status == ResultStatus.COMPLETED
    assert response.media_directive.source.origin == ("https://cdn.example/media/photo.png")
    captured = capsys.readouterr()
    assert "never-log-this" not in captured.err
    assert "never-log-this" not in str(container.audit.query())
