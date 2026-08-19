from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from assistant.command_center.contracts import DestinationBinding, SourceBinding
from assistant.storage.database import SettingsStore


class CommandCenterBindingStore:
    SOURCE_KEY = "command_center.source_bindings.v1"
    DESTINATION_KEY = "command_center.destination_bindings.v1"

    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings

    def list_sources(self) -> tuple[SourceBinding, ...]:
        return self._load(self.SOURCE_KEY, SourceBinding)

    def list_destinations(self) -> tuple[DestinationBinding, ...]:
        return self._load(self.DESTINATION_KEY, DestinationBinding)

    def get_source(self, binding_id: UUID) -> SourceBinding:
        try:
            return next(value for value in self.list_sources() if value.binding_id == binding_id)
        except StopIteration as exc:
            raise KeyError(f"Unknown source binding: {binding_id}") from exc

    def get_destination(self, binding_id: UUID) -> DestinationBinding:
        try:
            return next(
                value for value in self.list_destinations() if value.binding_id == binding_id
            )
        except StopIteration as exc:
            raise KeyError(f"Unknown destination binding: {binding_id}") from exc

    def save_source(self, binding: SourceBinding) -> SourceBinding:
        validated = SourceBinding.model_validate(binding).model_copy(
            update={"updated_at": datetime.now(UTC)}
        )
        values = [
            value for value in self.list_sources() if value.binding_id != validated.binding_id
        ]
        values.append(validated)
        self._save(self.SOURCE_KEY, values)
        return validated

    def save_destination(self, binding: DestinationBinding) -> DestinationBinding:
        validated = DestinationBinding.model_validate(binding).model_copy(
            update={"updated_at": datetime.now(UTC)}
        )
        values = [
            value for value in self.list_destinations() if value.binding_id != validated.binding_id
        ]
        values.append(validated)
        self._save(self.DESTINATION_KEY, values)
        return validated

    def delete_source(self, binding_id: UUID) -> SourceBinding:
        binding = self.get_source(binding_id)
        self._save(
            self.SOURCE_KEY,
            [value for value in self.list_sources() if value.binding_id != binding_id],
        )
        return binding

    def delete_destination(self, binding_id: UUID) -> DestinationBinding:
        binding = self.get_destination(binding_id)
        self._save(
            self.DESTINATION_KEY,
            [value for value in self.list_destinations() if value.binding_id != binding_id],
        )
        return binding

    def _load(self, key: str, model):
        payload = self.settings.get(key, [])
        if not isinstance(payload, list):
            return ()
        values = []
        for item in payload:
            try:
                values.append(model.model_validate(item))
            except (TypeError, ValidationError, ValueError):
                continue
        return tuple(
            sorted(values, key=lambda value: (value.display_name.casefold(), str(value.binding_id)))
        )

    def _save(self, key: str, values: list) -> None:
        self.settings.set(
            key,
            [
                value.model_dump(mode="json")
                for value in sorted(
                    values,
                    key=lambda item: (item.display_name.casefold(), str(item.binding_id)),
                )
            ],
        )
