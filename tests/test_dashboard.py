from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from assistant.bootstrap import build_container
from assistant.contracts import PipelineState
from assistant.dashboard.builtins import default_dashboard_layout
from assistant.dashboard.contracts import (
    DashboardCard,
    DashboardLayout,
    MetricDefinition,
    MetricPresentation,
    MetricRequest,
    MetricSnapshot,
    MetricState,
    MetricValueKind,
)
from assistant.dashboard.registry import DashboardProviderRegistry
from assistant.dashboard.service import DashboardService
from assistant.dashboard.storage import DashboardLayoutStore
from assistant.storage.database import SettingsStore


@pytest.mark.asyncio
async def test_default_dashboard_aggregates_privacy_bounded_local_metrics(settings):
    container = build_container(settings)
    container.metrics.record(
        correlation_id="completed",
        stage="execution",
        duration_ms=20,
        outcome="completed",
    )
    container.metrics.record(
        correlation_id="failed",
        stage="execution",
        duration_ms=80,
        outcome="failed",
    )
    container.dashboard.set_pipeline_state(PipelineState.THINKING)

    layout = container.dashboard.layout()
    snapshots = await container.dashboard.refresh()
    by_metric = {card.metric_id: snapshots[card.card_id] for card in layout.cards}

    assert [card.metric_id for card in layout.cards] == [
        "assistant.status",
        "assistant.request_outcomes",
        "assistant.execution_latency",
        "automations.summary",
        "assistant.recent_activity",
    ]
    assert SettingsStore(container.database).get(DashboardLayoutStore.KEY) is not None
    assert by_metric["assistant.status"].value == "Thinking"
    assert by_metric["assistant.request_outcomes"].value == 2
    assert by_metric["assistant.execution_latency"].value == 50
    assert "p95 80.0 ms" in by_metric["assistant.execution_latency"].summary
    assert by_metric["automations.summary"].state == MetricState.READY
    assert by_metric["assistant.recent_activity"].value == "Failed"
    assert all("transcript" not in snapshot.model_dump() for snapshot in snapshots.values())


def test_dashboard_layout_store_falls_back_without_overwriting_invalid_payload(settings):
    container = build_container(settings)
    values = SettingsStore(container.database)
    values.set(DashboardLayoutStore.KEY, {"layout_version": 1, "cards": "invalid"})
    store = DashboardLayoutStore(values, default_dashboard_layout)

    layout = store.load()

    assert layout == default_dashboard_layout()
    assert values.get(DashboardLayoutStore.KEY) == {
        "layout_version": 1,
        "cards": "invalid",
    }


@pytest.mark.asyncio
async def test_dashboard_provider_failures_are_isolated_and_redacted(settings):
    container = build_container(settings)
    registry = DashboardProviderRegistry()
    registry.register(_WorkingProvider())
    registry.register(_FailingProvider())
    settings_store = SettingsStore(container.database)
    layout = DashboardLayout(
        cards=(
            DashboardCard(
                card_id=uuid4(),
                metric_id="test.working",
                presentation=MetricPresentation.NUMBER,
                position=0,
            ),
            DashboardCard(
                card_id=uuid4(),
                metric_id="test.failing",
                presentation=MetricPresentation.NUMBER,
                position=1,
            ),
        )
    )
    layouts = DashboardLayoutStore(settings_store, lambda: layout)
    layouts.save(layout)
    service = DashboardService(registry, layouts)

    snapshots = await service.refresh()
    working, failing = (
        snapshots[layout.cards[0].card_id],
        snapshots[layout.cards[1].card_id],
    )

    assert working.value == 42
    assert working.state == MetricState.READY
    assert failing.state == MetricState.ERROR
    assert failing.summary == "Metric could not be refreshed."
    assert "private-token" not in failing.model_dump_json()


def test_dashboard_layout_customization_is_validated_and_persisted(settings):
    container = build_container(settings)
    current = container.dashboard.layout()
    customized = DashboardLayout(
        cards=tuple(
            card.model_copy(
                update={
                    "position": position,
                    "title": "My activity"
                    if card.metric_id == "assistant.recent_activity"
                    else None,
                    "size": "wide" if position == 0 else "small",
                    "refresh_seconds": 300,
                }
            )
            for position, card in enumerate(reversed(current.cards))
        )
    )

    saved = container.dashboard.save_layout(customized)

    assert saved.cards[0].metric_id == "assistant.recent_activity"
    assert saved.cards[0].title == "My activity"
    assert saved.cards[0].size == "wide"
    assert container.dashboard.layout() == saved
    audit = container.audit.query(event_type="dashboard_layout_saved")
    assert audit[0]["details"]["card_count"] == len(customized.cards)
    assert "My activity" not in str(audit[0]["details"])

    invalid = DashboardLayout(
        cards=(
            current.cards[0].model_copy(
                update={
                    "metric_id": "unknown.metric",
                    "position": 0,
                }
            ),
        )
    )
    with pytest.raises(KeyError, match="Unknown dashboard metric"):
        container.dashboard.save_layout(invalid)


def test_dashboard_rejects_an_unsupported_presentation(settings):
    container = build_container(settings)
    status_card = next(
        card for card in container.dashboard.layout().cards if card.metric_id == "assistant.status"
    )
    invalid = DashboardLayout(
        cards=(
            status_card.model_copy(
                update={
                    "presentation": MetricPresentation.NUMBER,
                    "position": 0,
                }
            ),
        )
    )

    with pytest.raises(ValueError, match="does not support number"):
        container.dashboard.save_layout(invalid)


def test_dashboard_contracts_reject_unknown_fields_and_non_json_options():
    with pytest.raises(ValidationError):
        DashboardCard(
            card_id=uuid4(),
            metric_id="test.value",
            presentation=MetricPresentation.NUMBER,
            position=0,
            unknown_field=True,
        )
    with pytest.raises(ValidationError):
        DashboardCard(
            card_id=uuid4(),
            metric_id="test.value",
            presentation=MetricPresentation.NUMBER,
            position=0,
            options={"unsafe": object()},
        )


class _WorkingProvider:
    provider_id = "test.working"
    permission_subject = "dashboard.test.working"
    remote = False

    def definitions(self) -> tuple[MetricDefinition, ...]:
        return (
            MetricDefinition(
                metric_id="test.working",
                provider_id=self.provider_id,
                display_name="Working",
                description="A working test provider.",
                source_label="Test",
                value_kind=MetricValueKind.NUMBER,
                supported_presentations=(MetricPresentation.NUMBER,),
                default_presentation=MetricPresentation.NUMBER,
            ),
        )

    def validate_card(self, card, connection) -> None:
        assert connection is None

    def required_capabilities(self, definition, connection, options):
        return ()

    def permission_subject_for(self, connection):
        return self.permission_subject

    async def health(self, connection):
        raise NotImplementedError

    async def collect(self, request: MetricRequest, connection=None) -> MetricSnapshot:
        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=42,
        )


class _FailingProvider:
    provider_id = "test.failing"
    permission_subject = "dashboard.test.failing"
    remote = False

    def definitions(self) -> tuple[MetricDefinition, ...]:
        return (
            MetricDefinition(
                metric_id="test.failing",
                provider_id=self.provider_id,
                display_name="Failing",
                description="A failing test provider.",
                source_label="Test",
                value_kind=MetricValueKind.NUMBER,
                supported_presentations=(MetricPresentation.NUMBER,),
                default_presentation=MetricPresentation.NUMBER,
            ),
        )

    def validate_card(self, card, connection) -> None:
        assert connection is None

    def required_capabilities(self, definition, connection, options):
        return ()

    def permission_subject_for(self, connection):
        return self.permission_subject

    async def health(self, connection):
        raise NotImplementedError

    async def collect(self, request: MetricRequest, connection=None) -> MetricSnapshot:
        raise RuntimeError("private-token")
