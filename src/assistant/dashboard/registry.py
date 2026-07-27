from __future__ import annotations

from assistant.dashboard.contracts import MetricDefinition
from assistant.dashboard.providers import DashboardMetricProvider


class DashboardProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, DashboardMetricProvider] = {}
        self._metrics: dict[str, tuple[DashboardMetricProvider, MetricDefinition]] = {}

    def register(self, provider: DashboardMetricProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"Dashboard provider already registered: {provider.provider_id}")
        definitions = provider.definitions()
        if not definitions:
            raise ValueError("Dashboard providers must declare at least one metric")
        for definition in definitions:
            if definition.provider_id != provider.provider_id:
                raise ValueError(
                    f"Metric {definition.metric_id} declares the wrong dashboard provider"
                )
            if definition.metric_id in self._metrics:
                raise ValueError(f"Dashboard metric already registered: {definition.metric_id}")
        self._providers[provider.provider_id] = provider
        for definition in definitions:
            self._metrics[definition.metric_id] = (provider, definition)

    def resolve(self, metric_id: str) -> tuple[DashboardMetricProvider, MetricDefinition]:
        try:
            return self._metrics[metric_id]
        except KeyError as exc:
            raise KeyError(f"Unknown dashboard metric: {metric_id}") from exc

    def provider(self, provider_id: str) -> DashboardMetricProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"Unknown dashboard provider: {provider_id}") from exc

    def definitions(self) -> tuple[MetricDefinition, ...]:
        return tuple(
            definition
            for _, definition in sorted(
                self._metrics.values(),
                key=lambda item: (item[1].source_label.casefold(), item[1].display_name.casefold()),
            )
        )
