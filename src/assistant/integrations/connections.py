from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, field_validator

from assistant.contracts import StrictModel


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    MISSING_SCOPES = "missing_scopes"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    ERROR = "error"


def normalize_scopes(values: Iterable[str]) -> tuple[str, ...]:
    scopes = {str(value).strip() for value in values if str(value).strip()}
    return tuple(sorted(scopes))


@dataclass(frozen=True)
class ConnectionStatus:
    """Provider-neutral account state safe for Hub and UI consumption.

    ``connected`` remains a concrete field for compatibility with the original
    Calendar/Gmail status objects. ``usable`` is the stricter signal used by
    the Hub: an authenticated account with missing scopes stays connected but
    cannot enable an extension until the user grants the required access.
    """

    connected: bool
    account: str | None = None
    healthy: bool | None = None
    message: str | None = None
    provider_id: str | None = None
    profile_id: UUID | None = None
    granted_scopes: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    state: ConnectionState | None = None

    def __post_init__(self) -> None:
        granted = normalize_scopes(self.granted_scopes)
        required = normalize_scopes(self.required_scopes)
        object.__setattr__(self, "granted_scopes", granted)
        object.__setattr__(self, "required_scopes", required)
        state = self.state
        if state is None:
            if self.connected and set(required).difference(granted):
                state = ConnectionState.MISSING_SCOPES
            elif self.connected:
                state = ConnectionState.CONNECTED
            else:
                state = ConnectionState.DISCONNECTED
        elif self.connected and state == ConnectionState.DISCONNECTED:
            state = ConnectionState.CONNECTED
        object.__setattr__(self, "state", ConnectionState(state))

    @property
    def missing_scopes(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.required_scopes).difference(self.granted_scopes)))

    @property
    def authenticated(self) -> bool:
        return self.connected or self.state == ConnectionState.MISSING_SCOPES

    @property
    def usable(self) -> bool:
        return bool(
            self.connected and self.state == ConnectionState.CONNECTED and not self.missing_scopes
        )

    def for_requirements(
        self,
        required_scopes: Iterable[str],
        *,
        provider_id: str | None = None,
        profile_id: UUID | None = None,
    ) -> ConnectionStatus:
        required = normalize_scopes((*self.required_scopes, *required_scopes))
        state = self.state
        if self.connected and set(required).difference(self.granted_scopes):
            state = ConnectionState.MISSING_SCOPES
        elif self.connected and state == ConnectionState.MISSING_SCOPES:
            state = ConnectionState.CONNECTED
        return replace(
            self,
            provider_id=provider_id or self.provider_id,
            profile_id=profile_id or self.profile_id,
            required_scopes=required,
            state=state,
        )

    @classmethod
    def coerce(
        cls,
        value: Any,
        *,
        provider_id: str | None = None,
        profile_id: UUID | None = None,
        required_scopes: Iterable[str] = (),
    ) -> ConnectionStatus:
        if isinstance(value, cls):
            return value.for_requirements(
                required_scopes,
                provider_id=provider_id,
                profile_id=profile_id,
            )
        status = cls(
            connected=bool(getattr(value, "connected", False)),
            account=getattr(value, "account", None),
            healthy=getattr(value, "healthy", None),
            message=getattr(value, "message", None),
            provider_id=provider_id or getattr(value, "provider_id", None),
            profile_id=profile_id or getattr(value, "profile_id", None),
            granted_scopes=tuple(getattr(value, "granted_scopes", ()) or ()),
            required_scopes=tuple(getattr(value, "required_scopes", ()) or ()),
            state=getattr(value, "state", None),
        )
        return status.for_requirements(required_scopes)


class ConnectionProfile(StrictModel):
    """Non-secret metadata for one account/workspace/provider connection."""

    profile_id: UUID = Field(default_factory=uuid4)
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    account: str | None = Field(default=None, max_length=320)
    enabled: bool = True
    endpoint: str | None = Field(default=None, max_length=1000)
    configuration: dict[str, JsonValue] = Field(default_factory=dict)
    secret_references: dict[str, str] = Field(default_factory=dict)
    granted_scopes: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("granted_scopes")
    @classmethod
    def _normalize_granted_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return normalize_scopes(value)

    @field_validator("configuration")
    @classmethod
    def _configuration_cannot_contain_credentials(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        sensitive_names = {
            "access_token",
            "api_key",
            "client_secret",
            "password",
            "refresh_token",
            "secret",
            "token",
        }

        def check(item: JsonValue) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if key.casefold() in sensitive_names:
                        raise ValueError("Connection credentials must use SecretVault references")
                    check(nested)
            elif isinstance(item, list):
                for nested in item:
                    check(nested)

        check(value)
        return value


@runtime_checkable
class ConnectionService(Protocol):
    def status(self, **kwargs: Any) -> Any: ...

    async def test(self) -> Any: ...

    async def disconnect(self) -> Any: ...


@dataclass(frozen=True)
class ConnectionBinding:
    """Bind an extension to one provider profile and its required OAuth scopes."""

    service: ConnectionService
    provider_id: str
    required_scopes: tuple[str, ...] = ()
    profile_id: UUID | None = None
    dialog_entry_point: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
            for character in self.provider_id
        ):
            raise ValueError("Invalid connection provider ID")
        service_provider_id = getattr(self.service, "provider_id", None)
        if service_provider_id is not None and service_provider_id != self.provider_id:
            raise ValueError("Connection binding provider ID does not match its service")
        object.__setattr__(self, "required_scopes", normalize_scopes(self.required_scopes))
        if self.dialog_entry_point is not None:
            module, separator, name = self.dialog_entry_point.partition(":")
            if not separator or not module or not name:
                raise ValueError("Connection dialog entry point must use module:Object syntax")

    def status(self) -> ConnectionStatus:
        status_method = self.service.status
        signature = inspect.signature(status_method)
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        kwargs: dict[str, Any] = {}
        if accepts_kwargs or "profile_id" in signature.parameters:
            kwargs["profile_id"] = self.profile_id
        if accepts_kwargs or "required_scopes" in signature.parameters:
            kwargs["required_scopes"] = self.required_scopes
        raw = status_method(**kwargs)
        return ConnectionStatus.coerce(
            raw,
            provider_id=self.provider_id,
            profile_id=self.profile_id,
            required_scopes=self.required_scopes,
        )

    @property
    def connection_key(self) -> tuple[str, UUID | None]:
        return self.provider_id, self.profile_id
