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
from assistant.storage.database import ItemRow, SettingsStore, now


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
    container.metrics.record(
        correlation_id="completed",
        stage="request.total",
        duration_ms=100,
        outcome="answer",
        dimensions={"interaction": "typed"},
    )
    container.metrics.record(
        correlation_id="failed",
        stage="request.total",
        duration_ms=300,
        outcome="error",
        dimensions={"interaction": "voice"},
    )
    container.metrics.record(
        correlation_id="completed",
        stage="model.total",
        duration_ms=40,
        outcome="planner.grounded",
    )
    container.metrics.record(
        correlation_id="resource",
        stage="resource.memory",
        value=128 * 1024 * 1024,
        outcome="sampled",
    )
    container.metrics.record(
        correlation_id="resource",
        stage="resource.queue_depth",
        value=2,
        outcome="sampled",
    )
    container.metrics.record(
        correlation_id="failed",
        stage="model.total",
        duration_ms=60,
        outcome="planner.grounded",
    )
    container.metrics.record(
        correlation_id="cloud",
        stage="cloud.tokens",
        value=123,
        outcome="completed",
        dimensions={"provider": "openai", "model": "gpt-test"},
    )
    container.metrics.record(
        correlation_id="cloud",
        stage="cloud.cost",
        value=0.0042,
        outcome="completed",
        dimensions={"provider": "openai", "model": "gpt-test"},
    )
    container.dashboard.set_pipeline_state(PipelineState.THINKING)

    layout = container.dashboard.layout()
    snapshots = await container.dashboard.refresh()
    by_metric = {card.metric_id: snapshots[card.card_id] for card in layout.cards}

    assert [card.metric_id for card in layout.cards] == [
        "assistant.status",
        "assistant.request_outcomes",
        "assistant.cloud_usage",
        "assistant.typed_request_latency",
        "assistant.voice_request_latency",
        "assistant.model_latency",
        "assistant.execution_latency",
        "assistant.resource_usage",
        "automations.summary",
        "tasks.summary",
        "assistant.recent_activity",
    ]
    assert SettingsStore(container.database).get(DashboardLayoutStore.KEY) is not None
    assert by_metric["assistant.status"].value == "Thinking"
    assert by_metric["assistant.request_outcomes"].value == 2
    assert by_metric["assistant.cloud_usage"].value == 123
    assert "estimated cost $0.004200" in by_metric["assistant.cloud_usage"].summary
    assert by_metric["assistant.typed_request_latency"].value == 100
    assert "p95 100.0 ms" in by_metric["assistant.typed_request_latency"].summary
    assert by_metric["assistant.voice_request_latency"].value == 300
    assert "p95 300.0 ms" in by_metric["assistant.voice_request_latency"].summary
    assert by_metric["assistant.model_latency"].value == 50
    assert "p95 60.0 ms" in by_metric["assistant.model_latency"].summary
    assert by_metric["assistant.execution_latency"].value == 50
    assert "p95 80.0 ms" in by_metric["assistant.execution_latency"].summary
    assert by_metric["assistant.resource_usage"].value == 128
    assert "2 active requests" in by_metric["assistant.resource_usage"].summary
    assert by_metric["automations.summary"].state == MetricState.READY
    assert by_metric["tasks.summary"].state == MetricState.EMPTY
    assert by_metric["assistant.recent_activity"].value == "Failed"
    assert all("transcript" not in snapshot.model_dump() for snapshot in snapshots.values())


@pytest.mark.asyncio
async def test_cloud_dashboard_preserves_configured_zero_cost_details(settings):
    container = build_container(settings)
    container.metrics.record(
        correlation_id="zero-cost",
        stage="cloud.tokens",
        value=1,
        outcome="completed",
        dimensions={"provider": "openai", "model": "gpt-free"},
    )
    container.metrics.record(
        correlation_id="zero-cost",
        stage="cloud.cost",
        value=0.0,
        outcome="completed",
        dimensions={"provider": "openai", "model": "gpt-free"},
    )

    layout = container.dashboard.layout()
    snapshots = await container.dashboard.refresh()
    card = next(card for card in layout.cards if card.metric_id == "assistant.cloud_usage")

    assert snapshots[card.card_id].details[0].value == "1 tokens  •  $0.000000"


@pytest.mark.asyncio
async def test_tasks_dashboard_metric_shows_open_and_completed_tasks(settings):
    container = build_container(settings)
    with container.database.session() as session:
        session.add(ItemRow(kind="task", title="Review the release", body="", created_at=now()))
        session.add(
            ItemRow(
                kind="task",
                title="Ship the checklist",
                body="",
                completed=True,
                created_at=now(),
            )
        )

    layout = container.dashboard.layout()
    snapshots = await container.dashboard.refresh()
    task_card = next(card for card in layout.cards if card.metric_id == "tasks.summary")
    snapshot = snapshots[task_card.card_id]

    assert snapshot.value == 1
    assert snapshot.summary == "1 open  •  1 completed"
    assert [(detail.label, detail.value) for detail in snapshot.details] == [
        ("Open #1", "Review the release"),
        ("Done #2", "Ship the checklist"),
    ]


def test_dashboard_layout_store_migrates_default_cards_once(settings):
    container = build_container(settings)
    values = SettingsStore(container.database)
    defaults = default_dashboard_layout()
    legacy = defaults.model_copy(
        update={
            "cards": tuple(card for card in defaults.cards if card.metric_id != "tasks.summary")
        }
    )
    values.set(DashboardLayoutStore.KEY, legacy.model_dump(mode="json"))
    store = DashboardLayoutStore(values, default_dashboard_layout)

    migrated = store.load()

    assert [card.metric_id for card in migrated.cards].count("tasks.summary") == 1
    assert values.get(DashboardLayoutStore.DEFAULT_MIGRATION_KEY) is True

    removed = DashboardLayout(
        cards=tuple(card for card in migrated.cards if card.metric_id != "tasks.summary")
    )
    store.save(removed)

    assert "tasks.summary" not in {card.metric_id for card in store.load().cards}


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
