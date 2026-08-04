from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from assistant.integrations.connections import ConnectionStatus, normalize_scopes
from assistant.integrations.gmail import GMAIL_SCOPE
from assistant.integrations.google_calendar import (
    CALENDAR_SCOPE,
    DEFAULT_GOOGLE_CALENDAR_CLIENT_ID,
)
from assistant.integrations.google_drive import DRIVE_READ_SCOPE
from assistant.integrations.oauth import (
    OAuthLoopbackMessages,
    OAuthSecretNames,
    OAuthTokenManager,
    PKCEAuthorization,
    run_loopback_authorization,
)
from assistant.storage.secrets import SecretVault

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_ACCOUNT_SCOPES = ("openid", "email", "profile")
DEFAULT_GOOGLE_CLIENT_ID = DEFAULT_GOOGLE_CALENDAR_CLIENT_ID
OAUTH_CLIENT_ID_NAME = "google_oauth_client_id"
OAUTH_CLIENT_SECRET_NAME = "google_oauth_client_secret"
GOOGLE_SECRETS = OAuthSecretNames(
    client_id="google_client_id",
    client_secret="google_client_secret",
    refresh_token="google_refresh_token",
    access_token="google_access_token",
    expires_at="google_access_token_expires_at",
    account="google_account",
    granted_scopes="google_granted_scopes",
    configuration_client_id=OAUTH_CLIENT_ID_NAME,
    configuration_client_secret=OAUTH_CLIENT_SECRET_NAME,
)
MAX_USERINFO_RESPONSE_BYTES = 64 * 1024


class GoogleError(RuntimeError):
    """A bounded Google account failure safe to surface to the application."""

    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"Google account request failed ({status}): {self.reason}")


def _google_error(status: int, reason: str, recoverable: bool) -> GoogleError:
    return GoogleError(status, reason, recoverable=recoverable)


@dataclass(frozen=True)
class _LegacyGoogleAuthorization:
    client_id: str
    client_secret: str
    refresh_token: str
    access_token: str
    expires_at: str
    account: str
    granted_scopes: str
    configuration_client_id: str
    configuration_client_secret: str
    default_scopes: tuple[str, ...]


_LEGACY_AUTHORIZATIONS = (
    _LegacyGoogleAuthorization(
        client_id="google_calendar_client_id",
        client_secret="google_calendar_client_secret",
        refresh_token="google_calendar_refresh_token",
        access_token="google_calendar_access_token",
        expires_at="google_calendar_access_token_expires_at",
        account="google_calendar_account",
        granted_scopes="google_calendar_granted_scopes",
        configuration_client_id="google_calendar_oauth_client_id",
        configuration_client_secret="google_calendar_oauth_client_secret",
        default_scopes=(CALENDAR_SCOPE,),
    ),
    _LegacyGoogleAuthorization(
        client_id="gmail_client_id",
        client_secret="gmail_client_secret",
        refresh_token="gmail_refresh_token",
        access_token="gmail_access_token",
        expires_at="gmail_access_token_expires_at",
        account="gmail_account",
        granted_scopes="gmail_granted_scopes",
        configuration_client_id="gmail_oauth_client_id",
        configuration_client_secret="gmail_oauth_client_secret",
        default_scopes=(GMAIL_SCOPE,),
    ),
    _LegacyGoogleAuthorization(
        client_id="google_drive_client_id",
        client_secret="google_drive_client_secret",
        refresh_token="google_drive_refresh_token",
        access_token="google_drive_access_token",
        expires_at="google_drive_access_token_expires_at",
        account="google_drive_account",
        granted_scopes="google_drive_granted_scopes",
        configuration_client_id="google_drive_oauth_client_id",
        configuration_client_secret="google_drive_oauth_client_secret",
        default_scopes=(DRIVE_READ_SCOPE,),
    ),
)


def _legacy_scopes(vault: SecretVault, name: str, defaults: Iterable[str]) -> tuple[str, ...]:
    raw = vault.get(name)
    if not raw:
        return normalize_scopes(defaults)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw.split()
    if isinstance(value, list):
        return normalize_scopes(str(item) for item in value)
    return normalize_scopes(defaults)


def migrate_legacy_google_authorization(vault: SecretVault) -> bool:
    """Copy one unambiguous legacy Google authorization into shared storage.

    Older releases stored one refresh token per Google product. The shared
    service must not silently choose between different Google accounts, so an
    automatic migration is performed only when all discovered legacy accounts
    agree. If several authorizations exist, the one with the broadest known
    scope set is copied and any missing product scopes can be granted through
    the normal incremental-consent flow.
    """

    if vault.get(GOOGLE_SECRETS.client_id) and vault.get(GOOGLE_SECRETS.refresh_token):
        return False

    candidates: list[dict[str, Any]] = []
    for legacy in _LEGACY_AUTHORIZATIONS:
        client_id = vault.get(legacy.client_id)
        refresh_token = vault.get(legacy.refresh_token)
        if not client_id or not refresh_token:
            continue
        candidates.append(
            {
                "legacy": legacy,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "account": vault.get(legacy.account),
                "scopes": _legacy_scopes(vault, legacy.granted_scopes, legacy.default_scopes),
            }
        )
    if not candidates:
        return False

    accounts = {
        account.casefold()
        for candidate in candidates
        if isinstance(account := candidate["account"], str) and account
    }
    if len(candidates) > 1 and (
        len(accounts) != 1
        or any(not isinstance(candidate["account"], str) for candidate in candidates)
    ):
        return False
    if len(accounts) > 1:
        return False

    candidate = max(
        candidates,
        key=lambda item: (
            len(item["scopes"]),
            bool(item["account"]),
        ),
    )
    legacy = candidate["legacy"]
    values = (
        (GOOGLE_SECRETS.refresh_token, candidate["refresh_token"]),
        (GOOGLE_SECRETS.granted_scopes, json.dumps(candidate["scopes"])),
        (GOOGLE_SECRETS.access_token, vault.get(legacy.access_token)),
        (GOOGLE_SECRETS.expires_at, vault.get(legacy.expires_at)),
        (GOOGLE_SECRETS.account, candidate["account"]),
        (GOOGLE_SECRETS.client_secret, vault.get(legacy.client_secret)),
        (OAUTH_CLIENT_ID_NAME, candidate["client_id"]),
        (OAUTH_CLIENT_SECRET_NAME, vault.get(legacy.configuration_client_secret)),
    )
    for name, value in values:
        if isinstance(value, str) and value:
            vault.set(name, value)
    # Keep the client ID as the final write, matching OAuthTokenManager's
    # durable commit-marker rule.
    vault.set(GOOGLE_SECRETS.client_id, candidate["client_id"])
    return True


class GoogleTokenManager(OAuthTokenManager):
    """One encrypted Google authorization shared by Google API adapters."""

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 20,
    ) -> None:
        migrate_legacy_google_authorization(vault)
        super().__init__(
            vault,
            names=GOOGLE_SECRETS,
            token_url=TOKEN_URL,
            revoke_url=REVOKE_URL,
            provider_name="Google",
            default_scopes=GOOGLE_ACCOUNT_SCOPES,
            opener=opener,
            clock=clock,
            timeout_seconds=timeout_seconds,
            error_factory=_google_error,
        )


GoogleConnectionStatus = ConnectionStatus


class GoogleConnectionService:
    """Own one Google account authorization for all Google Hub extensions."""

    provider_id = "google"

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        browser_open: Callable[[str], bool] = webbrowser.open,
        default_client_id: str = DEFAULT_GOOGLE_CLIENT_ID,
    ) -> None:
        self.vault = vault
        self.opener = opener
        self.browser_open = browser_open
        self.default_client_id = default_client_id.strip()

    def configured_client_id(self) -> str:
        migrate_legacy_google_authorization(self.vault)
        return (
            self.vault.get(OAUTH_CLIENT_ID_NAME)
            or self.vault.get(GOOGLE_SECRETS.client_id)
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
    ) -> GoogleConnectionStatus:
        tokens = self._tokens()
        connected = tokens.connected()
        required = normalize_scopes(required_scopes)
        status = GoogleConnectionStatus(
            connected=connected,
            account=tokens.account() if connected else None,
            healthy=healthy,
            message=message or ("Connected." if connected else "No Google account is connected."),
            provider_id=self.provider_id,
            profile_id=profile_id,
            granted_scopes=tokens.granted_scopes(),
            required_scopes=required,
        )
        if status.missing_scopes and message is None:
            return GoogleConnectionStatus(
                **{
                    **status.__dict__,
                    "message": "This Google account needs additional product access.",
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
    ) -> GoogleConnectionStatus:
        tokens = self._tokens()
        required = normalize_scopes(required_scopes)
        if tokens.connected() and not set(required).difference(tokens.granted_scopes()):
            return self.status(
                profile_id=profile_id,
                required_scopes=required,
                healthy=True,
                message="Google account is already connected.",
            )

        resolved_client_id = (client_id or self.configured_client_id()).strip()
        resolved_client_secret = client_secret.strip() if client_secret else None
        if resolved_client_secret is None:
            resolved_client_secret = self._client_secret_for(resolved_client_id)
        if resolved_client_secret:
            self.vault.set(OAUTH_CLIENT_ID_NAME, resolved_client_id)
            self.vault.set(OAUTH_CLIENT_SECRET_NAME, resolved_client_secret)
        scopes = normalize_scopes(
            (*GOOGLE_ACCOUNT_SCOPES, *tokens.granted_scopes(), *required)
        )
        return await asyncio.to_thread(
            self._connect,
            resolved_client_id,
            resolved_client_secret,
            scopes,
            required,
            timeout_seconds,
            profile_id,
        )

    def _client_secret_for(self, client_id: str) -> str | None:
        return self._tokens().client_secret(client_id)

    async def test(
        self,
        *,
        profile_id=None,
        required_scopes: tuple[str, ...] = (),
    ) -> GoogleConnectionStatus:
        status = self.status(profile_id=profile_id, required_scopes=required_scopes)
        if not status.connected:
            return status
        try:
            token = await self._tokens().access_token()
            account = await asyncio.to_thread(self._primary_account, token)
        except Exception:
            account = None
        healthy = bool(account)
        if account and status.account and account.casefold() != status.account.casefold():
            healthy = False
        if account and not status.account:
            self.vault.set(GOOGLE_SECRETS.account, account)
        return self.status(
            profile_id=profile_id,
            required_scopes=required_scopes,
            healthy=healthy,
            message=(
                "Google account connection is healthy."
                if healthy
                else "Google account could not be reached or authorization has expired."
            ),
        )

    async def disconnect(
        self,
        *,
        profile_id=None,
        required_scopes: tuple[str, ...] = (),
    ) -> GoogleConnectionStatus:
        await asyncio.to_thread(self._disconnect)
        return self.status(profile_id=profile_id, required_scopes=required_scopes)

    def _connect(
        self,
        client_id: str,
        client_secret: str | None,
        scopes: tuple[str, ...],
        required_scopes: tuple[str, ...],
        timeout_seconds: int,
        profile_id,
    ) -> GoogleConnectionStatus:
        if not client_id:
            raise ValueError("Google Desktop OAuth client ID is required")

        existing_account = self._tokens().account()

        def authorization_url(
            redirect_uri: str,
            pkce: PKCEAuthorization,
        ) -> str:
            values = {
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
            if existing_account:
                values["login_hint"] = existing_account
            return AUTH_URL + "?" + urllib.parse.urlencode(values)

        def exchange(code: str, verifier: str, redirect_uri: str):
            return self._complete_authorization(
                client_id=client_id,
                client_secret=client_secret,
                scopes=scopes,
                required_scopes=required_scopes,
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
                connected_heading="Google account connected",
                failed_heading="Google account was not connected",
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
        required_scopes: tuple[str, ...],
        code: str,
        verifier: str,
        redirect_uri: str,
        profile_id,
    ) -> GoogleConnectionStatus:
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
            raise GoogleError(401, "Google did not return the required OAuth tokens.")
        account = self._primary_account(access_token)
        existing_account = tokens.account()
        if (
            existing_account
            and account
            and existing_account.casefold() != account.casefold()
        ):
            raise GoogleError(
                409,
                "The Google account changed. Disconnect the current account before switching.",
            )
        account = account or existing_account
        response_scopes = payload.get("scope")
        granted = normalize_scopes(
            (
                *tokens.granted_scopes(),
                *scopes,
                *(response_scopes.split() if isinstance(response_scopes, str) else ()),
            )
        )
        payload = {**payload, "scope": " ".join(granted)}
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
            requested_scopes=granted,
        )
        return self.status(
            profile_id=profile_id,
            required_scopes=required_scopes,
            healthy=True,
            message="Google account connected for this extension.",
        )

    def _primary_account(self, access_token: str) -> str | None:
        request = urllib.request.Request(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=20) as response:
                payload = response.read(MAX_USERINFO_RESPONSE_BYTES + 1)
            if len(payload) > MAX_USERINFO_RESPONSE_BYTES:
                return None
            value = json.loads(payload.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(value, dict):
            return None
        account = value.get("email")
        return account[:320] if isinstance(account, str) and account else None

    def _disconnect(self) -> None:
        self._tokens().revoke()
        for legacy in _LEGACY_AUTHORIZATIONS:
            for name in (
                legacy.refresh_token,
                legacy.access_token,
                legacy.expires_at,
                legacy.account,
                legacy.granted_scopes,
            ):
                self.vault.delete(name)

    def _tokens(self) -> GoogleTokenManager:
        return GoogleTokenManager(self.vault, opener=self.opener)


def _connection_error_message(error: Exception) -> str:
    if isinstance(error, GoogleError):
        if "client_secret is missing" in error.reason.casefold():
            return (
                "Google requires this OAuth client's secret. Enter it once in the "
                "OAuth client settings and retry; Local Assistant stores it encrypted "
                "for future connections."
            )
        return error.reason
    if isinstance(error, PermissionError):
        return str(error)[:200]
    return "Local Assistant could not finish the secure Google account connection."
