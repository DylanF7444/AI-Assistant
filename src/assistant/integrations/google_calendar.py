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
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from assistant.storage.secrets import SecretVault

logger = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
# OAuth client IDs identify an application but are not secrets. This bundled
# Desktop client keeps account setup to a normal "Sign in with Google" flow.
DEFAULT_GOOGLE_CALENDAR_CLIENT_ID = (
    "613366814131-9cu8s8b18m4b72p18f1j37jihu8sdls2.apps.googleusercontent.com"
)
OAUTH_CLIENT_ID_NAME = "google_calendar_oauth_client_id"
OAUTH_CLIENT_SECRET_NAME = "google_calendar_oauth_client_secret"
TOKEN_SECRET_NAMES = (
    "google_calendar_refresh_token",
    "google_calendar_access_token",
    "google_calendar_access_token_expires_at",
    "google_calendar_account",
)


class GoogleCalendarError(RuntimeError):
    """A bounded Google Calendar failure safe to surface to the application."""

    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"Google Calendar request failed ({status}): {self.reason}")


@dataclass(frozen=True)
class GoogleCalendarConnectionStatus:
    connected: bool
    account: str | None = None
    healthy: bool | None = None
    message: str | None = None


class GoogleCalendarConnectionService:
    """Own Google OAuth setup without exposing credentials to extension code."""

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        browser_open: Callable[[str], bool] = webbrowser.open,
        default_client_id: str = DEFAULT_GOOGLE_CALENDAR_CLIENT_ID,
    ) -> None:
        self.vault = vault
        self.opener = opener
        self.browser_open = browser_open
        self.default_client_id = default_client_id.strip()

    def configured_client_id(self) -> str:
        """Return the durable OAuth client ID used for future connections."""

        return (
            self.vault.get(OAUTH_CLIENT_ID_NAME)
            or self.vault.get("google_calendar_client_id")
            or self.default_client_id
        )

    def has_client_secret(self) -> bool:
        """Report whether a client secret is already stored without exposing it."""

        return bool(self._client_secret_for(self.configured_client_id()))

    def status(self) -> GoogleCalendarConnectionStatus:
        connected = bool(
            self.vault.get("google_calendar_client_id")
            and self.vault.get("google_calendar_refresh_token")
        )
        return GoogleCalendarConnectionStatus(
            connected=connected,
            account=self.vault.get("google_calendar_account") if connected else None,
            message="Connected." if connected else "No Google Calendar account is connected.",
        )

    async def connect(
        self,
        client_id: str | None = None,
        *,
        client_secret: str | None = None,
        timeout_seconds: int = 180,
    ) -> GoogleCalendarConnectionStatus:
        resolved_client_id = (client_id or self.configured_client_id()).strip()
        resolved_client_secret = client_secret.strip() if client_secret else None
        if resolved_client_secret is None:
            resolved_client_secret = self._client_secret_for(resolved_client_id)
        if resolved_client_secret:
            # Persist client configuration before starting the browser flow. This
            # makes a failed first exchange recoverable without asking for the
            # secret again, while keeping account tokens separate from setup.
            self.vault.set(OAUTH_CLIENT_ID_NAME, resolved_client_id)
            self.vault.set(OAUTH_CLIENT_SECRET_NAME, resolved_client_secret)
        return await asyncio.to_thread(
            self._connect,
            resolved_client_id,
            resolved_client_secret,
            timeout_seconds,
        )

    def _client_secret_for(self, client_id: str) -> str | None:
        configured_id = self.vault.get(OAUTH_CLIENT_ID_NAME)
        if configured_id == client_id:
            configured_secret = self.vault.get(OAUTH_CLIENT_SECRET_NAME)
            if configured_secret:
                return configured_secret
        if self.vault.get("google_calendar_client_id") == client_id:
            return self.vault.get("google_calendar_client_secret")
        return None

    async def test(self) -> GoogleCalendarConnectionStatus:
        status = self.status()
        if not status.connected:
            return status
        health = await GoogleCalendarAdapter(
            GoogleCalendarTokenManager(self.vault, opener=self.opener),
            opener=self.opener,
        ).health()
        healthy = bool(health.get("healthy"))
        return GoogleCalendarConnectionStatus(
            connected=True,
            account=status.account,
            healthy=healthy,
            message=(
                "Google Calendar connection is healthy."
                if healthy
                else "Google Calendar could not be reached or authorization has expired."
            ),
        )

    async def disconnect(self) -> GoogleCalendarConnectionStatus:
        await asyncio.to_thread(self._disconnect)
        return self.status()

    def _connect(
        self,
        client_id: str,
        client_secret: str | None,
        timeout_seconds: int,
    ) -> GoogleCalendarConnectionStatus:
        if not client_id:
            raise ValueError("Google Desktop OAuth client ID is required")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)[:128]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        result: dict[str, Any] = {}
        completed = threading.Event()
        connection_service = self
        redirect_uri = ""

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/oauth/callback":
                    self.send_error(404)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("state", [""])[0] != state:
                    self._send_result(
                        400,
                        "Google Calendar was not connected",
                        "The authorization response could not be verified. "
                        "Return to Local Assistant and try again.",
                    )
                    return
                try:
                    if query.get("error"):
                        raise PermissionError(
                            f"Google authorization failed: {query['error'][0]}"
                        )
                    code = query.get("code", [""])[0]
                    if not code:
                        raise RuntimeError("Google authorization did not return a code")
                    status = connection_service._complete_authorization(
                        client_id=client_id,
                        client_secret=client_secret,
                        code=code,
                        verifier=verifier,
                        redirect_uri=redirect_uri,
                    )
                    result["status"] = status
                    self._send_result(
                        200,
                        "Google Calendar connected",
                        "You can close this tab and return to Local Assistant.",
                    )
                except Exception as error:
                    result["exception"] = error
                    message = _connection_error_message(error)
                    logger.warning(
                        "Google Calendar OAuth connection failed: %s",
                        message,
                        exc_info=True,
                    )
                    self._send_result(
                        400,
                        "Google Calendar was not connected",
                        f"{message} Return to Local Assistant and try again.",
                    )
                finally:
                    completed.set()

            def _send_result(self, status: int, heading: str, message: str) -> None:
                document = (
                    "<!doctype html><html lang=\"en\"><head>"
                    "<meta charset=\"utf-8\"><title>Local Assistant</title>"
                    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
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

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
        redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth/callback"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        authorization_url = (
            AUTH_URL
            + "?"
            + urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": CALENDAR_SCOPE,
                    "access_type": "offline",
                    "prompt": "consent",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                }
            )
        )
        try:
            if not self.browser_open(authorization_url):
                raise RuntimeError("The system browser could not be opened")
            if not completed.wait(max(30, min(timeout_seconds, 600))):
                raise TimeoutError("Google authorization timed out")
        finally:
            server.shutdown()
            server.server_close()
        failure = result.get("exception")
        if isinstance(failure, BaseException):
            raise failure
        status = result.get("status")
        if not isinstance(status, GoogleCalendarConnectionStatus):
            raise RuntimeError("Google authorization did not complete")
        return status

    def _complete_authorization(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> GoogleCalendarConnectionStatus:
        values = {
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if client_secret:
            values["client_secret"] = client_secret
        payload = self._post_form(TOKEN_URL, values)
        refresh_token = payload.get("refresh_token")
        access_token = payload.get("access_token")
        if not isinstance(refresh_token, str) or not isinstance(access_token, str):
            raise GoogleCalendarError(401, "Google did not return the required OAuth tokens.")
        account = self._primary_account(access_token)
        self.vault.set("google_calendar_access_token", access_token)
        self.vault.set(
            "google_calendar_access_token_expires_at",
            str(time.time() + int(payload.get("expires_in", 3600))),
        )
        if client_secret:
            self.vault.set(OAUTH_CLIENT_ID_NAME, client_id)
            self.vault.set(OAUTH_CLIENT_SECRET_NAME, client_secret)
            self.vault.set("google_calendar_client_secret", client_secret)
        else:
            self.vault.set(OAUTH_CLIENT_ID_NAME, client_id)
            self.vault.delete(OAUTH_CLIENT_SECRET_NAME)
            self.vault.delete("google_calendar_client_secret")
        self.vault.set("google_calendar_refresh_token", refresh_token)
        if account:
            self.vault.set("google_calendar_account", account)
        else:
            self.vault.delete("google_calendar_account")
        # Write the client ID last because status() uses it with the refresh
        # token as the durable connection commit marker.
        self.vault.set("google_calendar_client_id", client_id)
        return GoogleCalendarConnectionStatus(
            connected=True,
            account=account,
            healthy=True,
            message="Google Calendar account connected.",
        )

    def _primary_account(self, access_token: str) -> str | None:
        request = urllib.request.Request(
            f"{API_ROOT}/calendars/primary",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=20) as response:
                payload = json.load(response)
        except Exception:
            return None
        for key in ("id", "summary"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:320]
        return None

    def _disconnect(self) -> None:
        token = self.vault.get("google_calendar_refresh_token") or self.vault.get(
            "google_calendar_access_token"
        )
        if token:
            with contextlib.suppress(GoogleCalendarError, OSError):
                self._post_form(REVOKE_URL, {"token": token})
        # OAuth application credentials identify this Local Assistant build and
        # are retained so reconnecting remains a one-click flow. Disconnecting
        # removes only the user's authorization and account information.
        for name in TOKEN_SECRET_NAMES:
            self.vault.delete(name)

    def _post_form(self, url: str, values: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(values).encode("ascii"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with self.opener(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            raise _http_error(error) from None
        if not isinstance(payload, dict):
            raise GoogleCalendarError(502, "Google returned an invalid OAuth response.")
        return payload


class GoogleCalendarTokenManager:
    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 20,
    ) -> None:
        self.vault = vault
        self.opener = opener
        self.clock = clock
        self.timeout_seconds = timeout_seconds

    def connected(self) -> bool:
        return bool(
            self.vault.get("google_calendar_client_id")
            and self.vault.get("google_calendar_refresh_token")
        )

    async def access_token(self, *, force_refresh: bool = False) -> str:
        token = self.vault.get("google_calendar_access_token")
        expires_raw = self.vault.get("google_calendar_access_token_expires_at")
        try:
            expires_at = float(expires_raw or 0)
        except ValueError:
            expires_at = 0
        if token and not force_refresh and expires_at > self.clock() + 60:
            return token
        return await asyncio.to_thread(self._refresh)

    def _refresh(self) -> str:
        client_id = self.vault.get("google_calendar_client_id")
        refresh_token = self.vault.get("google_calendar_refresh_token")
        if not client_id or not refresh_token:
            raise GoogleCalendarError(
                401,
                "Google Calendar is not connected. Run the connection helper first.",
            )
        values = {
            "client_id": client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        client_secret = self.vault.get("google_calendar_client_secret")
        if not client_secret and self.vault.get(OAUTH_CLIENT_ID_NAME) == client_id:
            client_secret = self.vault.get(OAUTH_CLIENT_SECRET_NAME)
        if client_secret:
            values["client_secret"] = client_secret
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode(values).encode("ascii"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            raise _http_error(error) from None
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise GoogleCalendarError(401, "Google did not return an access token.")
        expires_in = payload.get("expires_in", 3600)
        try:
            expiry = self.clock() + max(60, int(expires_in))
        except (TypeError, ValueError):
            expiry = self.clock() + 3600
        self.vault.set("google_calendar_access_token", token)
        self.vault.set("google_calendar_access_token_expires_at", str(expiry))
        return token


class GoogleCalendarAdapter:
    def __init__(
        self,
        tokens: GoogleCalendarTokenManager,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout_seconds: float = 20,
    ) -> None:
        self.tokens = tokens
        self.opener = opener
        self.timeout_seconds = timeout_seconds

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self.tokens.access_token()
        try:
            return await asyncio.to_thread(self._request, method, path, token, query or {}, body)
        except GoogleCalendarError as error:
            if error.status != 401:
                raise
        token = await self.tokens.access_token(force_refresh=True)
        return await asyncio.to_thread(self._request, method, path, token, query or {}, body)

    async def health(self) -> dict[str, Any]:
        if not self.tokens.connected():
            return {"healthy": False, "error": "not_connected"}
        try:
            await self.request("GET", "/users/me/calendarList", query={"maxResults": 1})
            return {"healthy": True}
        except GoogleCalendarError as error:
            return {"healthy": False, "error": error.reason}

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        query: dict[str, Any],
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "://" in path:
            raise ValueError("Google Calendar API path must be relative")
        pairs = []
        for key, value in query.items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = str(value).lower()
            if isinstance(value, (list, tuple)):
                pairs.extend((key, item) for item in value)
            else:
                pairs.append((key, value))
        encoded_query = urllib.parse.urlencode(pairs, doseq=True)
        url = f"{API_ROOT}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise _http_error(error) from None
        if not payload:
            return {}
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise GoogleCalendarError(502, "Google returned an invalid response.")
        return value


def quote_id(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _http_error(error: urllib.error.HTTPError) -> GoogleCalendarError:
    reason = "The remote service rejected the request."
    try:
        payload = json.loads(error.read().decode("utf-8"))
        remote = payload.get("error", {})
        if isinstance(remote, dict):
            candidate = remote.get("message")
            if isinstance(candidate, str) and candidate:
                reason = candidate
        elif isinstance(remote, str) and remote:
            description = payload.get("error_description")
            reason = (
                description
                if isinstance(description, str) and description
                else remote.replace("_", " ")
            )
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        pass
    return GoogleCalendarError(
        error.code,
        reason,
        recoverable=error.code in {408, 429, 500, 502, 503, 504},
    )


def _connection_error_message(error: Exception) -> str:
    if isinstance(error, GoogleCalendarError):
        if "client_secret is missing" in error.reason.casefold():
            return (
                "Google requires this OAuth client's secret. Enter it once in the "
                "OAuth client settings and retry; Local Assistant stores it encrypted "
                "for future connections."
            )
        return error.reason
    if isinstance(error, PermissionError):
        return str(error)[:200]
    return "Local Assistant could not finish the secure Google connection."
