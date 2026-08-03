from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from assistant.integrations.connections import ConnectionStatus, normalize_scopes
from assistant.storage.secrets import SecretVault


class ApiTokenConnectionService:
    """Provider-neutral encrypted token lifecycle for reviewed read-only APIs."""

    def __init__(
        self,
        vault: SecretVault,
        *,
        provider_id: str,
        token_secret_name: str,
        account_secret_name: str | None = None,
        display_name: str,
    ) -> None:
        self.vault = vault
        self.provider_id = provider_id
        self.token_secret_name = token_secret_name
        self.account_secret_name = account_secret_name
        self.display_name = display_name

    def status(
        self,
        *,
        profile_id=None,
        required_scopes: tuple[str, ...] = (),
        healthy: bool | None = None,
        message: str | None = None,
    ) -> ConnectionStatus:
        connected = bool(self.vault.get(self.token_secret_name))
        return ConnectionStatus(
            connected=connected,
            account=self.account() if connected else None,
            healthy=healthy,
            message=message
            or (
                f"{self.display_name} connected."
                if connected
                else f"No {self.display_name} token is connected."
            ),
            provider_id=self.provider_id,
            profile_id=profile_id,
            required_scopes=normalize_scopes(required_scopes),
        )

    async def connect(
        self,
        token: str | None = None,
        *,
        account: str | None = None,
        required_scopes: tuple[str, ...] = (),
        profile_id=None,
    ) -> ConnectionStatus:
        resolved = (token or "").strip()
        if not resolved:
            raise ValueError(f"{self.display_name} token is required")
        previous_token = self.vault.get(self.token_secret_name)
        previous_account = self.account()
        self.vault.set(self.token_secret_name, resolved)
        try:
            probe = await self.probe()
            if not probe.get("healthy"):
                raise RuntimeError(str(probe.get("message") or f"{self.display_name} token was rejected"))
            resolved_account = account or _bounded_string(probe.get("account"))
            if resolved_account and self.account_secret_name:
                self.vault.set(self.account_secret_name, resolved_account)
            elif self.account_secret_name:
                self.vault.delete(self.account_secret_name)
        except Exception:
            if previous_token:
                self.vault.set(self.token_secret_name, previous_token)
                if self.account_secret_name:
                    if previous_account:
                        self.vault.set(self.account_secret_name, previous_account)
                    else:
                        self.vault.delete(self.account_secret_name)
            else:
                self.vault.delete(self.token_secret_name)
                if self.account_secret_name:
                    self.vault.delete(self.account_secret_name)
            raise
        return self.status(
            profile_id=profile_id,
            required_scopes=required_scopes,
            healthy=True,
            message=f"{self.display_name} connected.",
        )

    async def test(self) -> ConnectionStatus:
        status = self.status()
        if not status.connected:
            return status
        probe = await self.probe()
        healthy = bool(probe.get("healthy"))
        return self.status(
            healthy=healthy,
            message=(
                f"{self.display_name} connection is healthy."
                if healthy
                else str(probe.get("message") or f"{self.display_name} connection failed.")[:200]
            ),
        )

    async def disconnect(self, *, profile_id=None, required_scopes: tuple[str, ...] = ()) -> ConnectionStatus:
        self.vault.delete(self.token_secret_name)
        if self.account_secret_name:
            self.vault.delete(self.account_secret_name)
        return self.status(profile_id=profile_id, required_scopes=required_scopes)

    def account(self) -> str | None:
        if not self.account_secret_name:
            return None
        return self.vault.get(self.account_secret_name)

    def token(self) -> str | None:
        return self.vault.get(self.token_secret_name)

    async def probe(self) -> dict[str, Any]:
        return {"healthy": True}


class ApiTokenProvider:
    """AccessTokenProvider implementation backed by one encrypted token."""

    def __init__(self, service: ApiTokenConnectionService) -> None:
        self.service = service

    async def access_token(self, *, force_refresh: bool = False) -> str:
        token = self.service.token()
        if not token:
            raise RuntimeError(f"{self.service.display_name} is not connected")
        return token


def _bounded_string(value: Any) -> str | None:
    return value[:320] if isinstance(value, str) and value else None
