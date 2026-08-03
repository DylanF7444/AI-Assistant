from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from assistant.integrations.connections import normalize_scopes
from assistant.storage.secrets import SecretVault

ErrorFactory = Callable[[int, str, bool], Exception]

DEFAULT_MAX_OAUTH_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class OAuthSecretNames:
    client_id: str
    refresh_token: str
    access_token: str
    expires_at: str
    account: str | None = None
    client_secret: str | None = None
    granted_scopes: str | None = None
    configuration_client_id: str | None = None
    configuration_client_secret: str | None = None

    @property
    def authorization_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                self.refresh_token,
                self.access_token,
                self.expires_at,
                self.account,
                self.granted_scopes,
            )
            if name
        )


@dataclass(frozen=True)
class OAuthLoopbackMessages:
    connected_heading: str
    failed_heading: str
    connected_message: str = "You can close this tab and return to Local Assistant."
    invalid_state_message: str = (
        "The authorization response could not be verified. Return to Local Assistant and try again."
    )


@dataclass(frozen=True)
class PKCEAuthorization:
    state: str
    verifier: str
    challenge: str


def create_pkce_authorization() -> PKCEAuthorization:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return PKCEAuthorization(
        state=secrets.token_urlsafe(32),
        verifier=verifier,
        challenge=challenge,
    )


def run_loopback_authorization[T](
    *,
    authorization_url: Callable[[str, PKCEAuthorization], str],
    exchange: Callable[[str, str, str], T],
    browser_open: Callable[[str], bool],
    messages: OAuthLoopbackMessages,
    error_message: Callable[[Exception], str],
    logger: logging.Logger,
    timeout_seconds: int = 180,
    callback_path: str = "/oauth/callback",
) -> T:
    """Complete one state/PKCE-protected desktop OAuth callback on loopback."""

    if not callback_path.startswith("/") or "?" in callback_path or "#" in callback_path:
        raise ValueError("OAuth callback path must be an absolute path")
    pkce = create_pkce_authorization()
    result: dict[str, Any] = {}
    completed = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != callback_path:
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if query.get("state", [""])[0] != pkce.state:
                    raise PermissionError(messages.invalid_state_message)
                remote_error = query.get("error", [""])[0]
                if remote_error:
                    safe_error = "".join(
                        character
                        for character in remote_error[:80]
                        if character.isalnum() or character in "._- "
                    )
                    raise PermissionError(
                        f"Authorization was declined ({safe_error or 'provider_error'})."
                    )
                code = query.get("code", [""])[0]
                if not code:
                    raise RuntimeError("The authorization provider did not return a code.")
                result["value"] = exchange(code, pkce.verifier, redirect_uri)
                self._send_result(
                    200,
                    messages.connected_heading,
                    messages.connected_message,
                )
            except Exception as error:
                result["exception"] = error
                safe_message = error_message(error)[:300]
                logger.warning("OAuth loopback authorization failed: %s", safe_message)
                self._send_result(
                    400,
                    messages.failed_heading,
                    f"{safe_message} Return to Local Assistant and try again.",
                )
            finally:
                completed.set()

        def _send_result(self, status: int, heading: str, message: str) -> None:
            document = (
                '<!doctype html><html lang="en"><head>'
                '<meta charset="utf-8"><title>Local Assistant</title>'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                "</head><body><main>"
                f"<h1>{html.escape(heading)}</h1>"
                f"<p>{html.escape(message)}</p>"
                "</main></body></html>"
            ).encode()
            with contextlib.suppress(OSError):
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(document)))
                self.end_headers()
                self.wfile.write(document)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}{callback_path}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not browser_open(authorization_url(redirect_uri, pkce)):
            raise RuntimeError("The system browser could not be opened.")
        wait_seconds = max(30, min(int(timeout_seconds), 600))
        if not completed.wait(wait_seconds):
            raise TimeoutError("Authorization timed out.")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    failure = result.get("exception")
    if isinstance(failure, BaseException):
        raise failure
    if "value" not in result:
        raise RuntimeError("Authorization did not complete.")
    return result["value"]


class OAuthTokenManager:
    """Reusable encrypted OAuth token lifecycle with a durable commit marker."""

    def __init__(
        self,
        vault: SecretVault,
        *,
        names: OAuthSecretNames,
        token_url: str,
        revoke_url: str | None,
        provider_name: str,
        default_scopes: Iterable[str] = (),
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 20,
        error_factory: ErrorFactory | None = None,
        max_response_bytes: int = DEFAULT_MAX_OAUTH_RESPONSE_BYTES,
    ) -> None:
        self.vault = vault
        self.names = names
        self.token_url = _validate_https_endpoint(token_url)
        self.revoke_url = _validate_https_endpoint(revoke_url) if revoke_url else None
        self.provider_name = provider_name
        self.default_scopes = normalize_scopes(default_scopes)
        self.opener = opener
        self.clock = clock
        self.timeout_seconds = timeout_seconds
        self.error_factory = error_factory or self._default_error
        self.max_response_bytes = max(1024, int(max_response_bytes))

    def connected(self) -> bool:
        return bool(
            self.vault.get(self.names.client_id) and self.vault.get(self.names.refresh_token)
        )

    def account(self) -> str | None:
        return self.vault.get(self.names.account) if self.names.account else None

    def granted_scopes(self) -> tuple[str, ...]:
        if self.names.granted_scopes:
            raw = self.vault.get(self.names.granted_scopes)
            if raw:
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    value = raw.split()
                if isinstance(value, list):
                    return normalize_scopes(str(item) for item in value)
        return self.default_scopes if self.connected() else ()

    async def access_token(self, *, force_refresh: bool = False) -> str:
        token = self.vault.get(self.names.access_token)
        expires_raw = self.vault.get(self.names.expires_at)
        try:
            expires_at = float(expires_raw or 0)
        except ValueError:
            expires_at = 0
        if token and not force_refresh and expires_at > self.clock() + 60:
            return token
        return await asyncio.to_thread(self._refresh)

    def exchange_code(
        self,
        *,
        client_id: str,
        code: str,
        verifier: str,
        redirect_uri: str,
        client_secret: str | None = None,
    ) -> dict[str, Any]:
        values = {
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if client_secret:
            values["client_secret"] = client_secret
        return self._post_form(self.token_url, values)

    def save_authorization(
        self,
        payload: dict[str, Any],
        *,
        client_id: str,
        client_secret: str | None,
        account: str | None,
        requested_scopes: Iterable[str],
    ) -> None:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token") or self.vault.get(self.names.refresh_token)
        if not isinstance(access_token, str) or not access_token:
            raise self.error_factory(
                401, f"{self.provider_name} did not return an access token.", False
            )
        if not isinstance(refresh_token, str) or not refresh_token:
            raise self.error_factory(
                401, f"{self.provider_name} did not return a refresh token.", False
            )
        try:
            expires_at = self.clock() + max(60, int(payload.get("expires_in", 3600)))
        except (TypeError, ValueError):
            expires_at = self.clock() + 3600
        scopes = payload.get("scope")
        granted = (
            normalize_scopes(scopes.split())
            if isinstance(scopes, str) and scopes.strip()
            else normalize_scopes(requested_scopes)
        )
        self.vault.set(self.names.access_token, access_token)
        self.vault.set(self.names.expires_at, str(expires_at))
        if self.names.client_secret:
            if client_secret:
                self.vault.set(self.names.client_secret, client_secret)
            else:
                self.vault.delete(self.names.client_secret)
        self.vault.set(self.names.refresh_token, refresh_token)
        if self.names.account:
            if account:
                self.vault.set(self.names.account, account)
            else:
                self.vault.delete(self.names.account)
        if self.names.granted_scopes:
            self.vault.set(self.names.granted_scopes, json.dumps(granted))
        # The client ID is the durable commit marker and must be written last.
        self.vault.set(self.names.client_id, client_id)

    def revoke(self) -> None:
        token = self.vault.get(self.names.refresh_token) or self.vault.get(self.names.access_token)
        if token and self.revoke_url:
            with contextlib.suppress(Exception):
                self._post_form(self.revoke_url, {"token": token})
        for name in self.names.authorization_names:
            self.vault.delete(name)

    def client_secret(self, client_id: str) -> str | None:
        if self.names.client_secret:
            secret = self.vault.get(self.names.client_secret)
            if secret and self.vault.get(self.names.client_id) == client_id:
                return secret
        if (
            self.names.configuration_client_id
            and self.names.configuration_client_secret
            and self.vault.get(self.names.configuration_client_id) == client_id
        ):
            return self.vault.get(self.names.configuration_client_secret)
        return None

    def _refresh(self) -> str:
        client_id = self.vault.get(self.names.client_id)
        refresh_token = self.vault.get(self.names.refresh_token)
        if not client_id or not refresh_token:
            raise self.error_factory(
                401,
                f"{self.provider_name} is not connected. Connect an account first.",
                False,
            )
        values = {
            "client_id": client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        client_secret = self.client_secret(client_id)
        if client_secret:
            values["client_secret"] = client_secret
        payload = self._post_form(self.token_url, values)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise self.error_factory(
                401,
                f"{self.provider_name} did not return an access token.",
                False,
            )
        try:
            expiry = self.clock() + max(60, int(payload.get("expires_in", 3600)))
        except (TypeError, ValueError):
            expiry = self.clock() + 3600
        self.vault.set(self.names.access_token, token)
        self.vault.set(self.names.expires_at, str(expiry))
        scopes = payload.get("scope")
        if self.names.granted_scopes and isinstance(scopes, str) and scopes.strip():
            self.vault.set(
                self.names.granted_scopes,
                json.dumps(normalize_scopes(scopes.split())),
            )
        return token

    def _post_form(self, url: str, values: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(values).encode("ascii"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            status, reason, recoverable = _http_error_details(error, self.provider_name)
            raise self.error_factory(status, reason, recoverable) from None
        except (OSError, TimeoutError) as error:
            raise self.error_factory(
                503,
                f"{self.provider_name} could not be reached.",
                True,
            ) from error
        if len(payload) > self.max_response_bytes:
            raise self.error_factory(
                502,
                f"{self.provider_name} returned an oversized OAuth response.",
                False,
            )
        try:
            value = json.loads(payload.decode("utf-8")) if payload else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise self.error_factory(
                502,
                f"{self.provider_name} returned an invalid OAuth response.",
                False,
            ) from error
        if not isinstance(value, dict):
            raise self.error_factory(
                502,
                f"{self.provider_name} returned an invalid OAuth response.",
                False,
            )
        return value

    @staticmethod
    def _default_error(status: int, reason: str, _recoverable: bool) -> Exception:
        return RuntimeError(f"OAuth request failed ({status}): {reason[:200]}")


def _validate_https_endpoint(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("OAuth endpoints must use HTTPS without embedded credentials")
    return url


def _http_error_details(
    error: urllib.error.HTTPError,
    provider_name: str,
) -> tuple[int, str, bool]:
    reason = f"{provider_name} rejected the request."
    try:
        raw = error.read(DEFAULT_MAX_OAUTH_RESPONSE_BYTES + 1)
        if len(raw) <= DEFAULT_MAX_OAUTH_RESPONSE_BYTES:
            payload = json.loads(raw.decode("utf-8"))
            remote = payload.get("error", {}) if isinstance(payload, dict) else {}
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
    return error.code, reason, error.code in {408, 429, 500, 502, 503, 504}
