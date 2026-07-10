from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class DeviceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: str = "1.0"


class DeviceIdentity(DeviceContract):
    device_id: UUID = Field(default_factory=uuid4)
    display_name: str
    public_key_fingerprint: str
    capabilities: tuple[str, ...] = ()
    revoked: bool = False


class DeviceHealth(DeviceContract):
    device_id: UUID
    online: bool
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, Any] = Field(default_factory=dict)


class DeviceCommand(DeviceContract):
    command_id: UUID = Field(default_factory=uuid4)
    device_id: UUID
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(seconds=30))
    nonce: str = Field(default_factory=lambda: secrets.token_hex(16))


class DeviceResult(DeviceContract):
    command_id: UUID
    success: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class DeviceTransport(Protocol):
    async def send(self, command: DeviceCommand) -> DeviceResult: ...
    async def health(self, device_id: UUID) -> DeviceHealth: ...


def sign_command(command: DeviceCommand, shared_key: bytes) -> str:
    payload = json.dumps(
        command.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hmac.new(shared_key, payload, hashlib.sha256).hexdigest()


class ReplayGuard:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def accept(self, command: DeviceCommand, signature: str, key: bytes) -> bool:
        if command.expires_at <= datetime.now(UTC):
            return False
        if command.nonce in self._seen:
            return False
        if not hmac.compare_digest(signature, sign_command(command, key)):
            return False
        self._seen.add(command.nonce)
        return True


class PairingManager:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[datetime, str]] = {}
        self._devices: dict[UUID, DeviceIdentity] = {}

    def begin(self, fingerprint: str) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._pending[code] = (datetime.now(UTC) + timedelta(minutes=2), fingerprint)
        return code

    def approve(self, code: str, display_name: str) -> DeviceIdentity:
        pending = self._pending.pop(code, None)
        if pending is None or pending[0] <= datetime.now(UTC):
            raise ValueError("Pairing code is invalid or expired")
        identity = DeviceIdentity(display_name=display_name, public_key_fingerprint=pending[1])
        self._devices[identity.device_id] = identity
        return identity

    def revoke(self, device_id: UUID) -> None:
        identity = self._devices[device_id]
        self._devices[device_id] = identity.model_copy(update={"revoked": True})
