from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from typing import Any

from assistant.integrations.connections import ConnectionStatus, normalize_scopes
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

GRAPH_API_ROOT = "https://graph.microsoft.com/v1.0"
AUTHORITY_ROOT = "https://login.microsoftonline.com/common"
AUTH_URL = f"{AUTHORITY_ROOT}/oauth2/v2.0/authorize"
TOKEN_URL = f"{AUTHORITY_ROOT}/oauth2/v2.0/token"

OPENID_SCOPE = "openid"
PROFILE_SCOPE = "profile"
OFFLINE_ACCESS_SCOPE = "offline_access"
OUTLOOK_MAIL_READ_SCOPE = "Mail.Read"
OUTLOOK_CALENDAR_READ_SCOPE = "Calendars.Read"
ONEDRIVE_READ_SCOPE = "Files.Read"
OUTLOOK_MAIL_WRITE_SCOPE = "Mail.ReadWrite"
OUTLOOK_MAIL_SEND_SCOPE = "Mail.Send"
OUTLOOK_CALENDAR_WRITE_SCOPE = "Calendars.ReadWrite"
ONEDRIVE_WRITE_SCOPE = "Files.ReadWrite"
MICROSOFT_GRAPH_BASE_SCOPES = (
    OPENID_SCOPE,
    PROFILE_SCOPE,
    OFFLINE_ACCESS_SCOPE,
)

# Microsoft does not provide a single client ID that can safely be bundled for
# arbitrary tenants. Users can supply one in the connection dialog or store it
# in the application configuration vault.
DEFAULT_MICROSOFT_GRAPH_CLIENT_ID = ""
OAUTH_CLIENT_ID_NAME = "microsoft_graph_oauth_client_id"
OAUTH_CLIENT_SECRET_NAME = "microsoft_graph_oauth_client_secret"
MICROSOFT_GRAPH_SECRETS = OAuthSecretNames(
    client_id="microsoft_graph_client_id",
    client_secret="microsoft_graph_client_secret",
    refresh_token="microsoft_graph_refresh_token",
    access_token="microsoft_graph_access_token",
    expires_at="microsoft_graph_access_token_expires_at",
    account="microsoft_graph_account",
    granted_scopes="microsoft_graph_granted_scopes",
    configuration_client_id=OAUTH_CLIENT_ID_NAME,
    configuration_client_secret=OAUTH_CLIENT_SECRET_NAME,
)


class MicrosoftGraphError(RuntimeError):
    """A bounded Microsoft Graph failure safe to surface to the application."""

    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"Microsoft Graph request failed ({status}): {self.reason}")


def _microsoft_graph_error(status: int, reason: str, recoverable: bool) -> MicrosoftGraphError:
    return MicrosoftGraphError(status, reason, recoverable=recoverable)


class MicrosoftGraphConnectionService:
    """Own Microsoft OAuth and keep access tokens outside extension code."""

    provider_id = "microsoft_graph"

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        browser_open: Callable[[str], bool] = webbrowser.open,
        default_client_id: str = DEFAULT_MICROSOFT_GRAPH_CLIENT_ID,
    ) -> None:
        self.vault = vault
        self.opener = opener
        self.browser_open = browser_open
        self.default_client_id = default_client_id.strip()

    def configured_client_id(self) -> str:
        return (
            self.vault.get(OAUTH_CLIENT_ID_NAME)
            or self.vault.get("microsoft_graph_client_id")
            or self.default_client_id
        )

    def status(
        self,
        *,
        profile_id=None,
        required_scopes: tuple[str, ...] = (),
        healthy: bool | None = None,
        message: str | None = None,
    ) -> ConnectionStatus:
        tokens = self._tokens()
        connected = tokens.connected()
        required = normalize_scopes(required_scopes)
        status = ConnectionStatus(
            connected=connected,
            account=tokens.account() if connected else None,
            healthy=healthy,
            message=message
            or ("Connected." if connected else "No Microsoft account is connected."),
            provider_id=self.provider_id,
            profile_id=profile_id,
            granted_scopes=tokens.granted_scopes(),
            required_scopes=required,
        )
        if status.missing_scopes and message is None:
            return ConnectionStatus(
                **{
                    **status.__dict__,
                    "message": "Microsoft account needs additional access.",
                }
            )
        return status

    async def connect(
        self,
        client_id: str | None = None,
        *,
        client_secret: str | None = None,
        required_scopes: tuple[str, ...] = (),
        profile_id=None,
        timeout_seconds: int = 180,
    ) -> ConnectionStatus:
        resolved_client_id = (client_id or self.configured_client_id()).strip()
        if not resolved_client_id:
            raise ValueError(
                "Microsoft OAuth client ID is required. Create a desktop/public client registration in Microsoft Entra ID and enter its ID in Advanced OAuth settings."
            )
        resolved_client_secret = client_secret.strip() if client_secret else None
        if resolved_client_secret is None:
            resolved_client_secret = self._tokens().client_secret(resolved_client_id)
        scopes = normalize_scopes((*MICROSOFT_GRAPH_BASE_SCOPES, *required_scopes))
        if resolved_client_secret:
            self.vault.set(OAUTH_CLIENT_ID_NAME, resolved_client_id)
            self.vault.set(OAUTH_CLIENT_SECRET_NAME, resolved_client_secret)
        return await asyncio.to_thread(
            self._connect,
            resolved_client_id,
            resolved_client_secret,
            scopes,
            timeout_seconds,
            profile_id,
        )

    async def test(self) -> ConnectionStatus:
        status = self.status()
        if not status.connected:
            return status
        health = await MicrosoftGraphAdapter(
            MicrosoftGraphTokenManager(self.vault, opener=self.opener),
            opener=self.opener,
        ).health()
        healthy = bool(health.get("healthy"))
        return self.status(
            healthy=healthy,
            message=(
                "Microsoft account connection is healthy."
                if healthy
                else "Microsoft Graph could not be reached or authorization has expired."
            ),
        )

    async def disconnect(self) -> ConnectionStatus:
        await asyncio.to_thread(self._disconnect)
        return self.status()

    def _connect(
        self,
        client_id: str,
        client_secret: str | None,
        scopes: tuple[str, ...],
        timeout_seconds: int,
        profile_id,
    ) -> ConnectionStatus:
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
                        "response_mode": "query",
                        "scope": " ".join(scopes),
                        "prompt": "select_account",
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
                scopes=scopes,
                code=code,
                verifier=verifier,
                redirect_uri=redirect_uri,
                profile_id=profile_id,
            )

        return run_loopback_authorization(
            authorization_url=authorization_url,
            exchange=exchange,
            browser_open=self.browser_open,
            messages=OAuthLoopbackMessages(
                connected_heading="Microsoft account connected",
                failed_heading="Microsoft account was not connected",
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
        scopes: tuple[str, ...],
        code: str,
        verifier: str,
        redirect_uri: str,
        profile_id,
    ) -> ConnectionStatus:
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
            raise MicrosoftGraphError(401, "Microsoft did not return the required OAuth tokens.")
        account = self._primary_account(access_token)
        self.vault.set(OAUTH_CLIENT_ID_NAME, client_id)
        if client_secret:
            self.vault.set(OAUTH_CLIENT_SECRET_NAME, client_secret)
        tokens.save_authorization(
            payload,
            client_id=client_id,
            client_secret=client_secret,
            account=account,
            requested_scopes=scopes,
        )
        return self.status(
            profile_id=profile_id,
            required_scopes=scopes,
            healthy=True,
            message="Microsoft account connected.",
        )

    def _primary_account(self, access_token: str) -> str | None:
        request = urllib.request.Request(
            f"{GRAPH_API_ROOT}/me?$select=mail,userPrincipalName,displayName",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=20) as response:
                payload = json.load(response)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        value = (
            payload.get("mail") or payload.get("userPrincipalName") or payload.get("displayName")
        )
        return value[:320] if isinstance(value, str) and value else None

    def _disconnect(self) -> None:
        # Microsoft Graph has no provider-neutral revoke endpoint for these
        # delegated desktop tokens. Remove local authorization material.
        with contextlib.suppress(Exception):
            self._tokens().revoke()

    def _tokens(self) -> MicrosoftGraphTokenManager:
        return MicrosoftGraphTokenManager(self.vault, opener=self.opener)


class MicrosoftGraphTokenManager(OAuthTokenManager):
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
            names=MICROSOFT_GRAPH_SECRETS,
            token_url=TOKEN_URL,
            revoke_url=None,
            provider_name="Microsoft Graph",
            default_scopes=MICROSOFT_GRAPH_BASE_SCOPES,
            opener=opener,
            clock=clock,
            timeout_seconds=timeout_seconds,
            error_factory=_microsoft_graph_error,
        )


class MicrosoftGraphAdapter:
    def __init__(
        self,
        tokens: MicrosoftGraphTokenManager,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout_seconds: float = 20,
    ) -> None:
        self.tokens = tokens
        self.opener = opener
        self.timeout_seconds = timeout_seconds
        self.client = ExternalJsonClient(
            GRAPH_API_ROOT,
            tokens,
            provider_name="Microsoft Graph",
            opener=opener,
            timeout_seconds=timeout_seconds,
            error_factory=_microsoft_graph_error,
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
            await self.request("GET", "/me", query={"$select": "id"})
            return {"healthy": True}
        except MicrosoftGraphError as error:
            return {"healthy": False, "error": error.reason}

    async def upload_bytes(
        self, path: str, payload: bytes, *, content_type: str = "application/octet-stream"
    ) -> dict[str, Any]:
        if not path.startswith("/") or "?" in path or "#" in path or "\\" in path:
            raise ValueError("Microsoft Graph upload path must be a safe relative path")
        token = await self.tokens.access_token()
        return await asyncio.to_thread(self._upload_bytes_sync, path, payload, content_type, token)

    def _upload_bytes_sync(
        self, path: str, payload: bytes, content_type: str, token: str
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{GRAPH_API_ROOT}{path}",
            data=payload,
            method="PUT",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
                "Accept": "application/json",
                "User-Agent": "LocalAssistant/0.1",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise MicrosoftGraphError(exc.code, "OneDrive upload failed.") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise MicrosoftGraphError(
                503, "OneDrive upload could not be reached.", recoverable=True
            ) from exc
        if len(raw) > 2 * 1024 * 1024:
            raise MicrosoftGraphError(502, "OneDrive returned an oversized upload response.")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MicrosoftGraphError(502, "OneDrive returned an invalid upload response.") from exc
        if not isinstance(value, dict):
            raise MicrosoftGraphError(502, "OneDrive returned an invalid upload response.")
        return value

    async def download_url(self, url: str, *, max_bytes: int) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        hostname = (parsed.hostname or "").casefold()
        allowed_suffixes = (".1drv.com", ".onedrive.com", ".sharepoint.com")
        if (
            parsed.scheme != "https"
            or not hostname
            or not any(hostname.endswith(suffix) for suffix in allowed_suffixes)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("OneDrive returned an untrusted download origin")
        return await asyncio.to_thread(self._download_url_sync, url, max_bytes)

    def _download_url_sync(self, url: str, max_bytes: int) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "LocalAssistant/0.1"})
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise MicrosoftGraphError(exc.code, "OneDrive download failed.") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise MicrosoftGraphError(
                503, "OneDrive download could not be reached.", recoverable=True
            ) from exc
        if len(payload) > max_bytes:
            raise MicrosoftGraphError(413, "OneDrive file exceeds the download safety limit.")
        return payload


def quote_id(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def odata_literal(value: str) -> str:
    """Escape a user value for an OData string literal."""

    return value.replace("'", "''")


def next_page_token(payload: dict[str, Any]) -> str | None:
    """Extract only the bounded skip token from Graph's continuation URL."""

    link = payload.get("@odata.nextLink")
    if not isinstance(link, str) or len(link) > 4096:
        return None
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
    except ValueError:
        return None
    token = values.get("$skiptoken", [None])[0]
    return token[:2048] if isinstance(token, str) and token else None


def _connection_error_message(error: Exception) -> str:
    if isinstance(error, MicrosoftGraphError):
        return error.reason
    if isinstance(error, PermissionError):
        return str(error)[:200]
    return "Local Assistant could not finish the secure Microsoft account connection."
