from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

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
from assistant.dashboard.storage import DashboardConnectionStore, DashboardLayoutStore
from assistant.storage.database import SettingsStore


@pytest.mark.asyncio
async def test_refresh_can_target_only_selected_cards(settings):
    container = build_container(settings)
    cards = container.dashboard.layout().cards

    snapshots = await container.dashboard.refresh(card_ids=(cards[0].card_id,))

    assert tuple(snapshots) == (cards[0].card_id,)
    assert container.dashboard.diagnostics()["refresh_started"] == 1
    assert container.dashboard.diagnostics()["ready"] == 1


@pytest.mark.asyncio
async def test_external_metric_refreshes_are_bounded_separately(settings):
    container = build_container(settings)
    provider = _ConcurrentRemoteProvider()
    registry = DashboardProviderRegistry()
    registry.register(provider)
    values = SettingsStore(container.database)
    layouts = DashboardLayoutStore(values, lambda: DashboardLayout(cards=()))
    connections = DashboardConnectionStore(values, container.secret_vault)
    service = DashboardService(
        registry,
        layouts,
        connections=connections,
        max_concurrency=8,
        max_external_concurrency=2,
    )
    connection = await service.create_connection(
        provider_id=provider.provider_id,
        display_name="Bounded remote",
        endpoint="https://metrics.example.com",
        secret_values={},
    )
    cards = tuple(
        DashboardCard(
            card_id=uuid4(),
            metric_id="concurrency.value",
            connection_id=connection.connection_id,
            presentation=MetricPresentation.NUMBER,
            position=index,
            options={"series": index},
        )
        for index in range(6)
    )
    service.save_layout(DashboardLayout(cards=cards))

    snapshots = await service.refresh()

    assert len(snapshots) == 6
    assert provider.max_active == 2


@pytest.mark.asyncio
async def test_identical_cards_share_one_provider_collection(settings):
    container = build_container(settings)
    provider = _SequencedLocalProvider()
    registry = DashboardProviderRegistry()
    registry.register(provider)
    cards = tuple(
        DashboardCard(
            card_id=uuid4(),
            metric_id="sequence.value",
            presentation=MetricPresentation.NUMBER,
            position=index,
        )
        for index in range(2)
    )
    layouts = DashboardLayoutStore(
        SettingsStore(container.database),
        lambda: DashboardLayout(cards=cards),
    )
    layouts.save(DashboardLayout(cards=cards))
    service = DashboardService(registry, layouts)

    snapshots = await service.refresh()

    assert provider.collect_count == 1
    assert {snapshot.value for snapshot in snapshots.values()} == {42}


@pytest.mark.asyncio
async def test_timeout_uses_safe_last_good_snapshot_and_diagnostics(settings):
    container = build_container(settings)
    provider = _SequencedLocalProvider()
    registry = DashboardProviderRegistry()
    registry.register(provider)
    card = DashboardCard(
        card_id=uuid4(),
        metric_id="sequence.value",
        presentation=MetricPresentation.NUMBER,
        position=0,
    )
    layouts = DashboardLayoutStore(
        SettingsStore(container.database),
        lambda: DashboardLayout(cards=(card,)),
    )
    layouts.save(DashboardLayout(cards=(card,)))
    service = DashboardService(registry, layouts, timeout_seconds=0.01)

    first = (await service.refresh())[card.card_id]
    provider.delay = True
    second = (await service.refresh())[card.card_id]

    assert first.state == MetricState.READY
    assert second.state == MetricState.STALE
    assert second.value == 42
    assert second.summary == "Refresh failed. Showing the last refreshed value."
    assert service.diagnostics()["timeout"] == 1
    assert service.diagnostics()["stale"] == 1


class _ConcurrentRemoteProvider:
    provider_id = "concurrency"
    permission_subject = "dashboard.concurrency"
    remote = True

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    def definitions(self):
        return (
            MetricDefinition(
                metric_id="concurrency.value",
                provider_id=self.provider_id,
                display_name="Concurrent value",
                description="A remote concurrency test metric.",
                source_label="Concurrency test",
                value_kind=MetricValueKind.NUMBER,
                supported_presentations=(MetricPresentation.NUMBER,),
                default_presentation=MetricPresentation.NUMBER,
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
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return MetricSnapshot(
                metric_id=request.metric_id,
                connection_id=connection.connection_id if connection else None,
                collected_at=datetime.now(UTC),
                state=MetricState.READY,
                value=request.options["series"],
            )
        finally:
            self.active -= 1


class _SequencedLocalProvider:
    provider_id = "sequence"
    permission_subject = "dashboard.sequence"
    remote = False

    def __init__(self) -> None:
        self.collect_count = 0
        self.delay = False

    def definitions(self):
        return (
            MetricDefinition(
                metric_id="sequence.value",
                provider_id=self.provider_id,
                display_name="Sequence value",
                description="A local sequence test metric.",
                source_label="Sequence test",
                value_kind=MetricValueKind.NUMBER,
                supported_presentations=(MetricPresentation.NUMBER,),
                default_presentation=MetricPresentation.NUMBER,
            ),
        )

    def validate_card(self, _card, connection):
        if connection is not None:
            raise ValueError("connection is not supported")

    def required_capabilities(self, _definition, _connection, _options):
        return ()

    def permission_subject_for(self, _connection):
        return self.permission_subject

    async def health(self, _connection):
        return ProviderHealth(healthy=True, checked_at=datetime.now(UTC))

    async def collect(
        self,
        request: MetricRequest,
        _connection: ProviderConnection | None,
    ) -> MetricSnapshot:
        self.collect_count += 1
        if self.delay:
            await asyncio.sleep(1)
        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=42,
        )
