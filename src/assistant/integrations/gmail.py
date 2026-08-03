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
from assistant.integrations.google_calendar import DEFAULT_GOOGLE_CALENDAR_CLIENT_ID
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

API_ROOT = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
# The OAuth desktop client identifies the Local Assistant application, not a
# specific Google API. Gmail keeps separate user tokens and authorization.
DEFAULT_GMAIL_CLIENT_ID = DEFAULT_GOOGLE_CALENDAR_CLIENT_ID
OAUTH_CLIENT_ID_NAME = "gmail_oauth_client_id"
OAUTH_CLIENT_SECRET_NAME = "gmail_oauth_client_secret"
TOKEN_SECRET_NAMES = (
    "gmail_refresh_token",
    "gmail_access_token",
    "gmail_access_token_expires_at",
    "gmail_account",
    "gmail_granted_scopes",
)
GMAIL_SECRETS = OAuthSecretNames(
    client_id="gmail_client_id",
    client_secret="gmail_client_secret",
    refresh_token="gmail_refresh_token",
    access_token="gmail_access_token",
    expires_at="gmail_access_token_expires_at",
    account="gmail_account",
    granted_scopes="gmail_granted_scopes",
    configuration_client_id=OAUTH_CLIENT_ID_NAME,
    configuration_client_secret=OAUTH_CLIENT_SECRET_NAME,
)


class GmailError(RuntimeError):
    """A bounded Gmail failure safe to surface to the application."""

    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"Gmail request failed ({status}): {self.reason}")


def _gmail_error(status: int, reason: str, recoverable: bool) -> GmailError:
    return GmailError(status, reason, recoverable=recoverable)


GmailConnectionStatus = ConnectionStatus


class GmailConnectionService:
    """Own Gmail OAuth setup without exposing credentials to skill arguments."""

    provider_id = "gmail"

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        browser_open: Callable[[str], bool] = webbrowser.open,
        default_client_id: str = DEFAULT_GMAIL_CLIENT_ID,
    ) -> None:
        self.vault = vault
        self.opener = opener
        self.browser_open = browser_open
        self.default_client_id = default_client_id.strip()

    def configured_client_id(self) -> str:
        return (
            self.vault.get(OAUTH_CLIENT_ID_NAME)
            or self.vault.get("gmail_client_id")
            or self.default_client_id
        )

    def has_client_secret(self) -> bool:
        return bool(self._client_secret_for(self.configured_client_id()))

    def status(
        self,
        *,
        profile_id=None,
        required_scopes: tuple[str, ...] = (),
        healthy: bool | None = None,
        message: str | None = None,
    ) -> GmailConnectionStatus:
        tokens = self._tokens()
        connected = tokens.connected()
        status = GmailConnectionStatus(
            connected=connected,
            account=tokens.account() if connected else None,
            healthy=healthy,
            message=message or ("Connected." if connected else "No Gmail account is connected."),
            provider_id=self.provider_id,
            profile_id=profile_id,
            granted_scopes=tokens.granted_scopes(),
            required_scopes=required_scopes or (GMAIL_SCOPE,),
        )
        if status.missing_scopes and message is None:
            return GmailConnectionStatus(
                **{
                    **status.__dict__,
                    "message": "Gmail needs additional account access.",
                }
            )
        return status

    async def connect(
        self,
        client_id: str | None = None,
        *,
        client_secret: str | None = None,
        timeout_seconds: int = 180,
    ) -> GmailConnectionStatus:
        resolved_client_id = (client_id or self.configured_client_id()).strip()
        resolved_client_secret = client_secret.strip() if client_secret else None
        if resolved_client_secret is None:
            resolved_client_secret = self._client_secret_for(resolved_client_id)
        if resolved_client_secret:
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

    async def test(self) -> GmailConnectionStatus:
        status = self.status()
        if not status.connected:
            return status
        health = await GmailAdapter(
            GmailTokenManager(self.vault, opener=self.opener),
            opener=self.opener,
        ).health()
        healthy = bool(health.get("healthy"))
        return self.status(
            healthy=healthy,
            message=(
                "Gmail connection is healthy."
                if healthy
                else "Gmail could not be reached or authorization has expired."
            ),
        )

    async def disconnect(self) -> GmailConnectionStatus:
        await asyncio.to_thread(self._disconnect)
        return self.status()

    def _connect(
        self,
        client_id: str,
        client_secret: str | None,
        timeout_seconds: int,
    ) -> GmailConnectionStatus:
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
                        "scope": GMAIL_SCOPE,
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
                connected_heading="Gmail connected",
                failed_heading="Gmail was not connected",
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
    ) -> GmailConnectionStatus:
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
            raise GmailError(401, "Google did not return the required OAuth tokens.")
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
            requested_scopes=(GMAIL_SCOPE,),
        )
        return self.status(healthy=True, message="Gmail account connected.")

    def _primary_account(self, access_token: str) -> str | None:
        request = urllib.request.Request(
            f"{API_ROOT}/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=20) as response:
                payload = json.load(response)
        except Exception:
            return None
        value = payload.get("emailAddress")
        return value[:320] if isinstance(value, str) and value else None

    def _disconnect(self) -> None:
        self._tokens().revoke()

    def _tokens(self) -> GmailTokenManager:
        return GmailTokenManager(self.vault, opener=self.opener)


class GmailTokenManager(OAuthTokenManager):
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
            names=GMAIL_SECRETS,
            token_url=TOKEN_URL,
            revoke_url=REVOKE_URL,
            provider_name="Gmail",
            default_scopes=(GMAIL_SCOPE,),
            opener=opener,
            clock=clock,
            timeout_seconds=timeout_seconds,
            error_factory=_gmail_error,
        )


class GmailAdapter:
    def __init__(
        self,
        tokens: GmailTokenManager,
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
            provider_name="Gmail",
            opener=opener,
            timeout_seconds=timeout_seconds,
            error_factory=_gmail_error,
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
            await self.request("GET", "/users/me/profile")
            return {"healthy": True}
        except GmailError as error:
            return {"healthy": False, "error": error.reason}


def quote_id(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _connection_error_message(error: Exception) -> str:
    if isinstance(error, GmailError):
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
