from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from collections.abc import Callable
from typing import Any

from assistant.integrations.connections import ConnectionStatus, normalize_scopes
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

API_ROOT = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DRIVE_METADATA_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DEFAULT_GOOGLE_DRIVE_CLIENT_ID = DEFAULT_GOOGLE_CALENDAR_CLIENT_ID
OAUTH_CLIENT_ID_NAME = "google_drive_oauth_client_id"
OAUTH_CLIENT_SECRET_NAME = "google_drive_oauth_client_secret"
GOOGLE_DRIVE_SECRETS = OAuthSecretNames(
    client_id="google_drive_client_id",
    client_secret="google_drive_client_secret",
    refresh_token="google_drive_refresh_token",
    access_token="google_drive_access_token",
    expires_at="google_drive_access_token_expires_at",
    account="google_drive_account",
    granted_scopes="google_drive_granted_scopes",
    configuration_client_id=OAUTH_CLIENT_ID_NAME,
    configuration_client_secret=OAUTH_CLIENT_SECRET_NAME,
)

FILE_FIELDS = (
    "id,name,mimeType,size,createdTime,modifiedTime,parents,webViewLink,description,"
    "trashed,driveId,md5Checksum"
)


class GoogleDriveError(RuntimeError):
    """A bounded Google Drive failure safe to surface to the application."""

    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"Google Drive request failed ({status}): {self.reason}")


def _google_drive_error(status: int, reason: str, recoverable: bool) -> GoogleDriveError:
    return GoogleDriveError(status, reason, recoverable=recoverable)


GoogleDriveConnectionStatus = ConnectionStatus


class GoogleDriveConnectionService:
    """Own Google Drive OAuth setup without exposing credentials to skills."""

    provider_id = "google_drive"

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        browser_open: Callable[[str], bool] = webbrowser.open,
        default_client_id: str = DEFAULT_GOOGLE_DRIVE_CLIENT_ID,
    ) -> None:
        self.vault = vault
        self.opener = opener
        self.browser_open = browser_open
        self.default_client_id = default_client_id.strip()

    def configured_client_id(self) -> str:
        return (
            self.vault.get(OAUTH_CLIENT_ID_NAME)
            or self.vault.get("google_drive_client_id")
            or self.default_client_id
        )

    def status(
        self,
        *,
        profile_id=None,
        required_scopes: tuple[str, ...] = (),
        healthy: bool | None = None,
        message: str | None = None,
    ) -> GoogleDriveConnectionStatus:
        tokens = self._tokens()
        connected = tokens.connected()
        required = normalize_scopes(required_scopes or (DRIVE_READ_SCOPE,))
        status = GoogleDriveConnectionStatus(
            connected=connected,
            account=tokens.account() if connected else None,
            healthy=healthy,
            message=message
            or ("Connected." if connected else "No Google Drive account is connected."),
            provider_id=self.provider_id,
            profile_id=profile_id,
            granted_scopes=tokens.granted_scopes(),
            required_scopes=required,
        )
        if status.missing_scopes and message is None:
            return GoogleDriveConnectionStatus(
                **{**status.__dict__, "message": "Google Drive needs additional account access."}
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
    ) -> GoogleDriveConnectionStatus:
        resolved_client_id = (client_id or self.configured_client_id()).strip()
        resolved_client_secret = client_secret.strip() if client_secret else None
        if resolved_client_secret is None:
            resolved_client_secret = self._client_secret_for(resolved_client_id)
        scopes = normalize_scopes(required_scopes or (DRIVE_READ_SCOPE,))
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

    def _client_secret_for(self, client_id: str) -> str | None:
        return self._tokens().client_secret(client_id)

    async def test(self) -> GoogleDriveConnectionStatus:
        status = self.status()
        if not status.connected:
            return status
        health = await GoogleDriveAdapter(
            GoogleDriveTokenManager(self.vault, opener=self.opener),
            opener=self.opener,
        ).health()
        healthy = bool(health.get("healthy"))
        return self.status(
            healthy=healthy,
            message=(
                "Google Drive connection is healthy."
                if healthy
                else "Google Drive could not be reached or authorization has expired."
            ),
        )

    async def disconnect(self) -> GoogleDriveConnectionStatus:
        await asyncio.to_thread(self._disconnect)
        return self.status()

    def _connect(
        self,
        client_id: str,
        client_secret: str | None,
        scopes: tuple[str, ...],
        timeout_seconds: int,
        profile_id,
    ) -> GoogleDriveConnectionStatus:
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
                        "scope": " ".join(scopes),
                        "access_type": "offline",
                        "include_granted_scopes": "true",
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
                connected_heading="Google Drive connected",
                failed_heading="Google Drive was not connected",
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
    ) -> GoogleDriveConnectionStatus:
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
            raise GoogleDriveError(401, "Google did not return the required OAuth tokens.")
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
            requested_scopes=scopes,
        )
        return self.status(
            profile_id=profile_id,
            required_scopes=scopes,
            healthy=True,
            message="Google Drive account connected.",
        )

    def _primary_account(self, access_token: str) -> str | None:
        request = urllib.request.Request(
            f"{API_ROOT}/about?fields=user(emailAddress,displayName)",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=20) as response:
                payload = json.load(response)
        except Exception:
            return None
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict):
            return None
        value = user.get("emailAddress") or user.get("displayName")
        return value[:320] if isinstance(value, str) and value else None

    def _disconnect(self) -> None:
        self._tokens().revoke()

    def _tokens(self) -> GoogleDriveTokenManager:
        return GoogleDriveTokenManager(self.vault, opener=self.opener)


class GoogleDriveTokenManager(OAuthTokenManager):
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
            names=GOOGLE_DRIVE_SECRETS,
            token_url=TOKEN_URL,
            revoke_url=REVOKE_URL,
            provider_name="Google Drive",
            default_scopes=(DRIVE_READ_SCOPE,),
            opener=opener,
            clock=clock,
            timeout_seconds=timeout_seconds,
            error_factory=_google_drive_error,
        )


class GoogleDriveAdapter:
    def __init__(
        self,
        tokens: GoogleDriveTokenManager,
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
            provider_name="Google Drive",
            opener=opener,
            timeout_seconds=timeout_seconds,
            error_factory=_google_drive_error,
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
            await self.request("GET", "/about", query={"fields": "user(emailAddress)"})
            return {"healthy": True}
        except GoogleDriveError as error:
            return {"healthy": False, "error": error.reason}

    async def download(self, file_id: str, *, max_bytes: int) -> bytes:
        token = await self.tokens.access_token()
        return await asyncio.to_thread(self._download_sync, file_id, token, max_bytes)

    def _download_sync(self, file_id: str, token: str, max_bytes: int) -> bytes:
        url = f"{API_ROOT}/files/{quote_id(file_id)}?alt=media"
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "LocalAssistant/0.1"},
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise GoogleDriveError(exc.code, "Google Drive download failed.") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise GoogleDriveError(
                503, "Google Drive download could not be reached.", recoverable=True
            ) from exc
        if len(payload) > max_bytes:
            raise GoogleDriveError(413, "Google Drive file exceeds the download safety limit.")
        return payload

    async def upload(
        self,
        *,
        name: str,
        payload: bytes,
        parent_id: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        token = await self.tokens.access_token()
        return await asyncio.to_thread(
            self._upload_sync, name, payload, parent_id, mime_type, token
        )

    def _upload_sync(
        self,
        name: str,
        payload: bytes,
        parent_id: str | None,
        mime_type: str | None,
        token: str,
    ) -> dict[str, Any]:
        boundary = f"assistant-{uuid.uuid4().hex}"
        metadata: dict[str, Any] = {"name": name}
        if parent_id:
            metadata["parents"] = [parent_id]
        content_type = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        body = (
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata, separators=(',', ':'))}\r\n"
                f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n"
            ).encode()
            + payload
            + f"\r\n--{boundary}--\r\n".encode()
        )
        request = urllib.request.Request(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields="
            + urllib.parse.quote(FILE_FIELDS, safe=","),
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
                "Accept": "application/json",
                "User-Agent": "LocalAssistant/0.1",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise GoogleDriveError(exc.code, "Google Drive upload failed.") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise GoogleDriveError(
                503, "Google Drive upload could not be reached.", recoverable=True
            ) from exc
        if len(raw) > 2 * 1024 * 1024:
            raise GoogleDriveError(502, "Google Drive returned an oversized upload response.")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleDriveError(
                502, "Google Drive returned an invalid upload response."
            ) from exc
        if not isinstance(value, dict):
            raise GoogleDriveError(502, "Google Drive returned an invalid upload response.")
        return value


def quote_id(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def drive_query_literal(value: str) -> str:
    """Escape a user search term for a Drive query string literal."""

    return value.replace("\\", "\\\\").replace("'", "\\'")


def _connection_error_message(error: Exception) -> str:
    if isinstance(error, GoogleDriveError):
        if "client_secret is missing" in error.reason.casefold():
            return (
                "Google requires this OAuth client's secret. Enter it once in the "
                "OAuth client settings and retry; Local Assistant stores it encrypted "
                "for future connections."
            )
        return error.reason
    if isinstance(error, PermissionError):
        return str(error)[:200]
    return "Local Assistant could not finish the secure Google Drive connection."
