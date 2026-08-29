from __future__ import annotations

from typing import Protocol

from assistant.calendar.contracts import (
    CalendarEvent,
    CalendarProviderDefinition,
    CalendarProviderStatus,
    CalendarQuery,
)
from assistant.contracts import PermissionScope


class CalendarProvider(Protocol):
    definition: CalendarProviderDefinition

    def validate_query(self, query: CalendarQuery) -> None: ...

    def required_capabilities(self, query: CalendarQuery) -> tuple[PermissionScope, ...]: ...

    def permission_subject_for(self, query: CalendarQuery) -> str: ...

    def status(self, query: CalendarQuery) -> CalendarProviderStatus: ...

    async def collect(self, query: CalendarQuery) -> tuple[CalendarEvent, ...]: ...


class CalendarProviderRegistry:
    """Registry that allows each future extension to contribute a calendar provider."""

    def __init__(self) -> None:
        self._providers: dict[str, CalendarProvider] = {}

    def register(self, provider: CalendarProvider) -> None:
        provider_id = provider.definition.provider_id
        if provider_id in self._providers:
            raise ValueError(f"Calendar provider already registered: {provider_id}")
        self._providers[provider_id] = provider

    def provider(self, provider_id: str) -> CalendarProvider:
        try:
            return self._providers[provider_id]
        except KeyError as error:
            raise KeyError(f"Unknown calendar provider: {provider_id}") from error

    def resolve(self, provider_id: str) -> CalendarProvider:
        """Alias used by extension contributions and other provider registries."""
        return self.provider(provider_id)

    def definitions(self) -> tuple[CalendarProviderDefinition, ...]:
        return tuple(
            provider.definition
            for provider in sorted(
                self._providers.values(),
                key=lambda item: item.definition.display_name.casefold(),
            )
        )
