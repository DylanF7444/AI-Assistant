from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

ErrorFactory = Callable[[int, str, bool], Exception]
Sleep = Callable[[float], Awaitable[Any]]

DEFAULT_MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_ERROR_BYTES = 64 * 1024
_RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
_IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS"}


class AccessTokenProvider(Protocol):
    async def access_token(self, *, force_refresh: bool = False) -> str: ...


@dataclass(frozen=True)
class _ExternalHttpFailure(Exception):
    status: int
    reason: str
    recoverable: bool
    retry_after_seconds: float | None = None


class ExternalJsonClient:
    """Bounded fixed-origin JSON client for reviewed first-party adapters."""

    def __init__(
        self,
        base_url: str,
        tokens: AccessTokenProvider,
        *,
        provider_name: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout_seconds: float = 20,
        max_response_bytes: int = DEFAULT_MAX_JSON_RESPONSE_BYTES,
        max_attempts: int = 3,
        max_retry_delay_seconds: float = 5,
        sleep: Sleep = asyncio.sleep,
        error_factory: ErrorFactory | None = None,
        user_agent: str = "LocalAssistant/0.1 Integration",
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self.tokens = tokens
        self.provider_name = provider_name
        self.opener = opener
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self.max_attempts = max(1, min(int(max_attempts), 5))
        self.max_retry_delay_seconds = max(0, min(float(max_retry_delay_seconds), 30))
        self.sleep = sleep
        self.error_factory = error_factory or self._default_error
        self.user_agent = user_agent

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        safe_to_retry: bool | None = None,
    ) -> dict[str, Any]:
        normalized_method = method.strip().upper()
        if not normalized_method.isalpha():
            raise ValueError("External API method is invalid")
        _validate_relative_path(path)
        retryable = (
            normalized_method in _IDEMPOTENT_METHODS
            if safe_to_retry is None
            else bool(safe_to_retry)
        )
        token = await self.tokens.access_token()
        refreshed = False
        attempt = 1
        while True:
            try:
                return await asyncio.to_thread(
                    self._request_once,
                    normalized_method,
                    path,
                    token,
                    dict(query or {}),
                    dict(body) if body is not None else None,
                    dict(headers or {}),
                )
            except _ExternalHttpFailure as error:
                if error.status == 401 and not refreshed:
                    token = await self.tokens.access_token(force_refresh=True)
                    refreshed = True
                    if retryable:
                        continue
                if retryable and error.recoverable and attempt < self.max_attempts:
                    delay = error.retry_after_seconds
                    if delay is None:
                        delay = min(0.25 * (2 ** (attempt - 1)), self.max_retry_delay_seconds)
                    else:
                        delay = min(max(0, delay), self.max_retry_delay_seconds)
                    attempt += 1
                    if delay:
                        await self.sleep(delay)
                    continue
                raise self.error_factory(
                    error.status,
                    error.reason[:200],
                    error.recoverable,
                ) from None

    def _request_once(
        self,
        method: str,
        path: str,
        token: str,
        query: dict[str, Any],
        body: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        pairs: list[tuple[str, Any]] = []
        for key, value in query.items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = str(value).lower()
            if isinstance(value, (list, tuple)):
                pairs.extend((str(key), item) for item in value)
            else:
                pairs.append((str(key), value))
        encoded_query = urllib.parse.urlencode(pairs, doseq=True)
        url = f"{self.base_url}{path}" + (f"?{encoded_query}" if encoded_query else "")
        request_headers = {
            **headers,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        request = urllib.request.Request(
            url,
            data=(
                json.dumps(body, separators=(",", ":")).encode("utf-8")
                if body is not None
                else None
            ),
            method=method,
            headers=request_headers,
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise _failure_from_http_error(error, self.provider_name) from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise _ExternalHttpFailure(
                503,
                f"{self.provider_name} could not be reached.",
                True,
            ) from error
        if len(payload) > self.max_response_bytes:
            raise _ExternalHttpFailure(
                502,
                f"{self.provider_name} returned a response larger than the safety limit.",
                False,
            )
        if not payload:
            return {}
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _ExternalHttpFailure(
                502,
                f"{self.provider_name} returned invalid JSON.",
                False,
            ) from error
        if not isinstance(value, dict):
            raise _ExternalHttpFailure(
                502,
                f"{self.provider_name} returned an invalid response object.",
                False,
            )
        return value

    @staticmethod
    def _default_error(status: int, reason: str, _recoverable: bool) -> Exception:
        return RuntimeError(f"External API request failed ({status}): {reason}")


def _validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("External API origins must use HTTPS without embedded credentials")
    return value.rstrip("/")


def _validate_relative_path(path: str) -> None:
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "://" in path
        or "\\" in path
        or "?" in path
        or "#" in path
        or any(ord(character) < 32 for character in path)
    ):
        raise ValueError("External API path must be a safe relative path")
    decoded = path
    for _attempt in range(8):
        if any(segment in {".", ".."} for segment in decoded.split("/")):
            raise ValueError("External API path must be a safe relative path")
        unquoted = urllib.parse.unquote(decoded)
        if unquoted == decoded:
            break
        decoded = unquoted
    else:
        raise ValueError("External API path must be a safe relative path")


def _failure_from_http_error(
    error: urllib.error.HTTPError,
    provider_name: str,
) -> _ExternalHttpFailure:
    reason = f"{provider_name} rejected the request."
    try:
        raw = error.read(DEFAULT_MAX_ERROR_BYTES + 1)
        if len(raw) <= DEFAULT_MAX_ERROR_BYTES:
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict):
                remote = payload.get("error", {})
                if isinstance(remote, dict):
                    candidate = remote.get("message") or remote.get("description")
                    if isinstance(candidate, str) and candidate:
                        reason = candidate[:200]
                elif isinstance(remote, str) and remote:
                    description = payload.get("error_description")
                    reason = (
                        description[:200]
                        if isinstance(description, str) and description
                        else remote.replace("_", " ")[:200]
                    )
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        pass
    retry_after = None
    if error.headers:
        raw_retry = error.headers.get("Retry-After")
        try:
            retry_after = float(raw_retry) if raw_retry is not None else None
        except (TypeError, ValueError):
            retry_after = None
    return _ExternalHttpFailure(
        error.code,
        reason,
        error.code in _RETRYABLE_STATUSES,
        retry_after,
    )
