from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError

from assistant.integrations.connections import ConnectionProfile, normalize_scopes
from assistant.storage.database import SettingsStore
from assistant.storage.secrets import SecretVault

_SECRET_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ConnectionProfileStore:
    """Persist provider profiles while keeping every credential in SecretVault."""

    PROFILES_KEY = "integration.connection_profiles.v1"
    ACTIVE_KEY = "integration.active_connection_profiles.v1"

    def __init__(self, settings: SettingsStore, secrets: SecretVault) -> None:
        self.settings = settings
        self.secrets = secrets

    def list(self, provider_id: str | None = None) -> tuple[ConnectionProfile, ...]:
        payload = self.settings.get(self.PROFILES_KEY, [])
        if not isinstance(payload, list):
            return ()
        profiles: list[ConnectionProfile] = []
        for value in payload:
            try:
                profile = ConnectionProfile.model_validate(value)
            except (TypeError, ValidationError, ValueError):
                continue
            if provider_id is None or profile.provider_id == provider_id:
                profiles.append(profile)
        return tuple(
            sorted(
                profiles,
                key=lambda item: (
                    item.provider_id,
                    item.display_name.casefold(),
                    str(item.profile_id),
                ),
            )
        )

    def get(self, profile_id: UUID) -> ConnectionProfile:
        try:
            return next(item for item in self.list() if item.profile_id == profile_id)
        except StopIteration as error:
            raise KeyError(f"Unknown connection profile: {profile_id}") from error

    def create(
        self,
        *,
        provider_id: str,
        display_name: str,
        account: str | None = None,
        endpoint: str | None = None,
        configuration: dict[str, JsonValue] | None = None,
        granted_scopes: tuple[str, ...] = (),
        secret_values: dict[str, str] | None = None,
        make_active: bool = True,
    ) -> ConnectionProfile:
        profile_id = uuid4()
        references = {
            name: self._secret_reference(profile_id, name) for name in (secret_values or {})
        }
        profile = ConnectionProfile(
            profile_id=profile_id,
            provider_id=provider_id,
            display_name=" ".join(display_name.split()),
            account=" ".join(account.split()) if account else None,
            endpoint=endpoint.strip() if endpoint else None,
            configuration=dict(configuration or {}),
            secret_references=references,
            granted_scopes=normalize_scopes(granted_scopes),
        )
        saved = self.save(profile, secret_values=secret_values)
        if make_active:
            self.set_active(provider_id, saved.profile_id)
        return saved

    def save(
        self,
        profile: ConnectionProfile,
        *,
        secret_values: dict[str, str] | None = None,
    ) -> ConnectionProfile:
        validated = ConnectionProfile.model_validate(profile)
        for logical_name, reference in validated.secret_references.items():
            self._validate_logical_secret_name(logical_name)
            if not reference.startswith(f"integration-{validated.profile_id.hex}-"):
                raise ValueError("Connection secret reference does not belong to this profile")
        pending_secrets: dict[str, str] = {}
        for logical_name, value in (secret_values or {}).items():
            self._validate_logical_secret_name(logical_name)
            if not isinstance(value, str):
                raise TypeError(f"Connection secret {logical_name} must be text")
            reference = validated.secret_references.get(logical_name)
            if reference is None:
                raise ValueError(f"Missing secret reference for {logical_name}")
            pending_secrets[reference] = value
        saved = validated.model_copy(update={"updated_at": datetime.now(UTC)})
        values = [item for item in self.list() if item.profile_id != saved.profile_id]
        values.append(saved)
        previous_secrets = {reference: self.secrets.get(reference) for reference in pending_secrets}
        try:
            for reference, value in pending_secrets.items():
                self.secrets.set(reference, value)
            self.settings.set(
                self.PROFILES_KEY,
                [
                    item.model_dump(mode="json")
                    for item in sorted(
                        values,
                        key=lambda value: (
                            value.provider_id,
                            value.display_name.casefold(),
                            str(value.profile_id),
                        ),
                    )
                ],
            )
        except Exception:
            for reference, previous in previous_secrets.items():
                if previous is None:
                    self.secrets.delete(reference)
                else:
                    self.secrets.set(reference, previous)
            raise
        return saved

    def delete(self, profile_id: UUID) -> ConnectionProfile:
        profile = self.get(profile_id)
        values = [item for item in self.list() if item.profile_id != profile_id]
        self.settings.set(
            self.PROFILES_KEY,
            [item.model_dump(mode="json") for item in values],
        )
        active = self._active_map()
        if active.get(profile.provider_id) == str(profile_id):
            active.pop(profile.provider_id, None)
            replacement = next(
                (item for item in values if item.provider_id == profile.provider_id),
                None,
            )
            if replacement is not None:
                active[profile.provider_id] = str(replacement.profile_id)
            self.settings.set(self.ACTIVE_KEY, active)
        for reference in profile.secret_references.values():
            self.secrets.delete(reference)
        return profile

    def active(self, provider_id: str) -> ConnectionProfile | None:
        raw = self._active_map().get(provider_id)
        if raw:
            try:
                profile = self.get(UUID(raw))
            except (KeyError, ValueError):
                profile = None
            if profile is not None and profile.provider_id == provider_id and profile.enabled:
                return profile
        return next((item for item in self.list(provider_id) if item.enabled), None)

    def set_active(self, provider_id: str, profile_id: UUID) -> ConnectionProfile:
        profile = self.get(profile_id)
        if profile.provider_id != provider_id:
            raise ValueError("Connection profile belongs to a different provider")
        active = self._active_map()
        active[provider_id] = str(profile_id)
        self.settings.set(self.ACTIVE_KEY, active)
        return profile

    def secret(self, profile: ConnectionProfile, logical_name: str) -> str | None:
        self._validate_logical_secret_name(logical_name)
        reference = profile.secret_references.get(logical_name)
        return self.secrets.get(reference) if reference else None

    def _active_map(self) -> dict[str, str]:
        payload = self.settings.get(self.ACTIVE_KEY, {})
        if not isinstance(payload, dict):
            return {}
        return {
            str(provider): str(profile_id)
            for provider, profile_id in payload.items()
            if isinstance(provider, str) and isinstance(profile_id, str)
        }

    @staticmethod
    def _validate_logical_secret_name(name: str) -> None:
        if not _SECRET_NAME.fullmatch(name):
            raise ValueError("Invalid connection secret name")

    @classmethod
    def _secret_reference(cls, profile_id: UUID, logical_name: str) -> str:
        cls._validate_logical_secret_name(logical_name)
        return f"integration-{profile_id.hex}-{logical_name}"
