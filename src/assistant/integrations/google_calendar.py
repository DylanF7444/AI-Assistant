from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from typing import Any

from assistant.integrations.connections import ConnectionStatus
from assistant.integrations.http import ExternalJsonClient
from assistant.integrations.oauth import (
    OAuthLoopbackMessages,
    OAuthSecretNames,
    OAuthTokenManager,
    PKCEAuthorization,
    run_loopback_authorization,
)
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
    "google_calendar_granted_scopes",
)
GOOGLE_CALENDAR_SECRETS = OAuthSecretNames(
    client_id="google_calendar_client_id",
    client_secret="google_calendar_client_secret",
    refresh_token="google_calendar_refresh_token",
    access_token="google_calendar_access_token",
    expires_at="google_calendar_access_token_expires_at",
    account="google_calendar_account",
    granted_scopes="google_calendar_granted_scopes",
    configuration_client_id=OAUTH_CLIENT_ID_NAME,
    configuration_client_secret=OAUTH_CLIENT_SECRET_NAME,
)


class GoogleCalendarError(RuntimeError):
    """A bounded Google Calendar failure safe to surface to the application."""

    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"Google Calendar request failed ({status}): {self.reason}")


def _google_calendar_error(
    status: int,
    reason: str,
    recoverable: bool,
) -> GoogleCalendarError:
    return GoogleCalendarError(status, reason, recoverable=recoverable)


GoogleCalendarConnectionStatus = ConnectionStatus


class GoogleCalendarConnectionService:
    """Own Google OAuth setup without exposing credentials to extension code."""

    provider_id = "google_calendar"

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

    def status(
        self,
        *,
        profile_id=None,
        required_scopes: tuple[str, ...] = (),
        healthy: bool | None = None,
        message: str | None = None,
    ) -> GoogleCalendarConnectionStatus:
        tokens = self._tokens()
        connected = tokens.connected()
        required = required_scopes or (CALENDAR_SCOPE,)
        status = GoogleCalendarConnectionStatus(
            connected=connected,
            account=tokens.account() if connected else None,
            healthy=healthy,
            message=message
            or ("Connected." if connected else "No Google Calendar account is connected."),
            provider_id=self.provider_id,
            profile_id=profile_id,
            granted_scopes=tokens.granted_scopes(),
            required_scopes=required,
        )
        if status.missing_scopes and message is None:
            return GoogleCalendarConnectionStatus(
                **{
                    **status.__dict__,
                    "message": "Google Calendar needs additional account access.",
                }
            )
        return status

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
        return self._tokens().client_secret(client_id)

    async def test(self) -> GoogleCalendarConnectionStatus:
        status = self.status()
        if not status.connected:
            return status
        health = await GoogleCalendarAdapter(
            GoogleCalendarTokenManager(self.vault, opener=self.opener),
            opener=self.opener,
        ).health()
        healthy = bool(health.get("healthy"))
        return self.status(
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

        def authorization_url(
            redirect_uri: str,
            pkce: PKCEAuthorization,
        ) -> str:
            return (
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
                        "code_challenge": pkce.challenge,
                        "code_challenge_method": "S256",
                        "state": pkce.state,
                    }
                )
            )

        def exchange(code: str, verifier: str, redirect_uri: str):
            return self._complete_authorization(
                client_id=client_id,
                client_secret=client_secret,
                code=code,
                verifier=verifier,
                redirect_uri=redirect_uri,
            )

        return run_loopback_authorization(
            authorization_url=authorization_url,
            exchange=exchange,
            browser_open=self.browser_open,
            messages=OAuthLoopbackMessages(
                connected_heading="Google Calendar connected",
                failed_heading="Google Calendar was not connected",
            ),
            error_message=_connection_error_message,
            logger=logger,
            timeout_seconds=timeout_seconds,
        )

    def _complete_authorization(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> GoogleCalendarConnectionStatus:
        tokens = self._tokens()
        payload = tokens.exchange_code(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            verifier=verifier,
            redirect_uri=redirect_uri,
        )
        access_token = payload.get("access_token")
        if not isinstance(access_token, str):
            raise GoogleCalendarError(401, "Google did not return the required OAuth tokens.")
        account = self._primary_account(access_token)
        self.vault.set(OAUTH_CLIENT_ID_NAME, client_id)
        if client_secret:
            self.vault.set(OAUTH_CLIENT_SECRET_NAME, client_secret)
        else:
            self.vault.delete(OAUTH_CLIENT_SECRET_NAME)
        tokens.save_authorization(
            payload,
            client_id=client_id,
            client_secret=client_secret,
            account=account,
            requested_scopes=(CALENDAR_SCOPE,),
        )
        return self.status(healthy=True, message="Google Calendar account connected.")

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
        # OAuth application credentials identify this Local Assistant build and
        # are retained so reconnecting remains a one-click flow. Disconnecting
        # removes only the user's authorization and account information.
        self._tokens().revoke()

    def _tokens(self) -> GoogleCalendarTokenManager:
        return GoogleCalendarTokenManager(self.vault, opener=self.opener)


class GoogleCalendarTokenManager(OAuthTokenManager):
    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 20,
    ) -> None:
        super().__init__(
            vault,
            names=GOOGLE_CALENDAR_SECRETS,
            token_url=TOKEN_URL,
            revoke_url=REVOKE_URL,
            provider_name="Google Calendar",
            default_scopes=(CALENDAR_SCOPE,),
            opener=opener,
            clock=clock,
            timeout_seconds=timeout_seconds,
            error_factory=_google_calendar_error,
        )


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
        self.client = ExternalJsonClient(
            API_ROOT,
            tokens,
            provider_name="Google Calendar",
            opener=opener,
            timeout_seconds=timeout_seconds,
            error_factory=_google_calendar_error,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(method, path, query=query, body=body)

    async def health(self) -> dict[str, Any]:
        if not self.tokens.connected():
            return {"healthy": False, "error": "not_connected"}
        try:
            await self.request("GET", "/users/me/calendarList", query={"maxResults": 1})
            return {"healthy": True}
        except GoogleCalendarError as error:
            return {"healthy": False, "error": error.reason}


def quote_id(value: str) -> str:
    return urllib.parse.quote(value, safe="")


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
