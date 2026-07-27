from __future__ import annotations

from typing import Protocol

from assistant.contracts import PermissionScope
from assistant.dashboard.contracts import (
    DashboardCard,
    MetricDefinition,
    MetricRequest,
    MetricSnapshot,
    ProviderConnection,
    ProviderHealth,
)


class DashboardMetricProvider(Protocol):
    provider_id: str
    permission_subject: str
    remote: bool

    def definitions(self) -> tuple[MetricDefinition, ...]: ...

    def validate_card(
        self,
        card: DashboardCard,
        connection: ProviderConnection | None,
    ) -> None: ...

    def required_capabilities(
        self,
        definition: MetricDefinition,
        connection: ProviderConnection | None,
        options: dict,
    ) -> tuple[PermissionScope, ...]: ...

    def permission_subject_for(self, connection: ProviderConnection | None) -> str: ...

    async def collect(
        self,
        request: MetricRequest,
        connection: ProviderConnection | None,
    ) -> MetricSnapshot: ...

    async def health(self, connection: ProviderConnection | None) -> ProviderHealth: ...
