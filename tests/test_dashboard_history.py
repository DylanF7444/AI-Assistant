from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from assistant.bootstrap import build_container
from assistant.dashboard.contracts import (
    DashboardCard,
    DashboardLayout,
    MetricDefinition,
    MetricPresentation,
    MetricRequest,
    MetricSnapshot,
    MetricState,
    MetricValueKind,
    ProviderConnection,
    ProviderHealth,
)
from assistant.dashboard.registry import DashboardProviderRegistry
from assistant.dashboard.service import DashboardService
from assistant.dashboard.storage import (
    DashboardConnectionStore,
    DashboardLayoutStore,
    DashboardSampleStore,
)
from assistant.storage.database import DashboardMetricSampleRow, SettingsStore


@pytest.mark.asyncio
async def test_numeric_history_is_opt_in_private_and_attached_to_snapshot(settings):
    container = build_container(settings)
    provider = _HistoryProvider([21.5, 22.0])
    service, samples = _service(container, provider)
    connection = await service.create_connection(
        provider_id=provider.provider_id,
        display_name="Test provider",
        endpoint="https://metrics.example.com",
        secret_values={},
        history_enabled=True,
    )
    card = _card(connection, entity_id="sensor.private_room")
    service.save_layout(DashboardLayout(cards=(card,)))

    first = (await service.refresh())[card.card_id]
    second = (await service.refresh())[card.card_id]

    assert first.trend[0].value == 21.5
    assert [point.value for point in second.trend] == [21.5, 22.0]
    assert samples.count() == 2
    with container.database.session() as session:
        rows = list(session.scalars(select(DashboardMetricSampleRow)))
    assert all(len(row.series_key) == 64 for row in rows)
    assert all("private_room" not in row.series_key for row in rows)
    assert all(row.metric_id == "history.value" for row in rows)


@pytest.mark.asyncio
async def test_history_rejects_text_and_stops_when_connection_opt_out(settings):
    container = build_container(settings)
    provider = _HistoryProvider(["open", 7])
    service, samples = _service(container, provider)
    connection = await service.create_connection(
        provider_id=provider.provider_id,
        display_name="Test provider",
        endpoint="https://metrics.example.com",
        secret_values={},
        history_enabled=True,
    )
    card = _card(connection)
    service.save_layout(DashboardLayout(cards=(card,)))

    text_snapshot = (await service.refresh())[card.card_id]
    service.set_connection_history_enabled(connection.connection_id, False)
    numeric_snapshot = (await service.refresh())[card.card_id]

    assert text_snapshot.trend == ()
    assert numeric_snapshot.trend == ()
    assert samples.count() == 0


@pytest.mark.asyncio
async def test_clear_delete_and_retention_remove_history(settings):
    container = build_container(settings)
    provider = _HistoryProvider([1])
    service, samples = _service(container, provider)
    connection = await service.create_connection(
        provider_id=provider.provider_id,
        display_name="Test provider",
        endpoint="https://metrics.example.com",
        secret_values={},
        history_enabled=True,
    )
    samples.record(
        timestamp=datetime.now(UTC) - timedelta(days=8),
        connection_id=connection.connection_id,
        metric_id="history.value",
        series_key="a" * 64,
        value=1,
    )
    samples.record(
        timestamp=datetime.now(UTC),
        connection_id=connection.connection_id,
        metric_id="history.value",
        series_key="a" * 64,
        value=2,
    )

    assert samples.prune(datetime.now(UTC) - timedelta(days=7)) == 1
    assert service.clear_connection_history(connection.connection_id) == 1
    assert samples.count() == 0

    card = _card(connection)
    service.save_layout(DashboardLayout(cards=(card,)))
    await service.refresh()
    assert samples.count() == 1
    service.delete_connection(connection.connection_id)
    assert samples.count() == 0


def _service(container, provider):
    registry = DashboardProviderRegistry()
    registry.register(provider)
    values = SettingsStore(container.database)
    layouts = DashboardLayoutStore(values, lambda: DashboardLayout(cards=()))
    layouts.save(DashboardLayout(cards=()))
    samples = DashboardSampleStore(container.database)
    return (
        DashboardService(
            registry,
            layouts,
            connections=DashboardConnectionStore(values, container.secret_vault),
            samples=samples,
            history_retention_days=7,
        ),
        samples,
    )


def _card(
    connection: ProviderConnection,
    *,
    entity_id: str = "sensor.value",
) -> DashboardCard:
    return DashboardCard(
        card_id=uuid4(),
        metric_id="history.value",
        connection_id=connection.connection_id,
        presentation=MetricPresentation.NUMBER,
        refresh_seconds=60,
        position=0,
        options={"entity_id": entity_id},
    )


class _HistoryProvider:
    provider_id = "history"
    permission_subject = "dashboard.history"
    remote = True

    def __init__(self, values: list[object]) -> None:
        self.values = values

    def definitions(self):
        return (
            MetricDefinition(
                metric_id="history.value",
                provider_id=self.provider_id,
                display_name="History value",
                description="A test metric that supports numeric history.",
                source_label="History test",
                value_kind=MetricValueKind.STATUS,
                supported_presentations=(MetricPresentation.NUMBER,),
                default_presentation=MetricPresentation.NUMBER,
                supports_history=True,
                requires_connection=True,
            ),
        )

    def validate_connection(self, _connection):
        return None

    def validate_card(self, _card, connection):
        if connection is None:
            raise ValueError("connection required")

    def required_capabilities(self, _definition, _connection, _options):
        return ()

    def permission_subject_for(self, _connection):
        return self.permission_subject

    async def health(self, _connection):
        return ProviderHealth(healthy=True, checked_at=datetime.now(UTC))

    async def collect(
        self,
        request: MetricRequest,
        connection: ProviderConnection | None,
    ) -> MetricSnapshot:
        return MetricSnapshot(
            metric_id=request.metric_id,
            connection_id=connection.connection_id if connection else None,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=self.values.pop(0),
        )
