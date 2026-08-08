from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from assistant.bootstrap import build_container
from assistant.logging import configure_logging
from assistant.observability import InteractionMode, MetricDimensions, RequestPath
from assistant.storage.database import Database, MetricsStore


def test_structured_logging_redacts_secrets(tmp_path):
    path = configure_logging(tmp_path)
    logger = logging.getLogger("assistant.test")
    logger.info(
        "event",
        extra={"correlation_id": "abc", "stage": "test", "token": "secret"},
    )
    for handler in logger.parent.handlers:
        handler.flush()
    payload = json.loads(path.read_text("utf-8").splitlines()[-1])
    assert payload["correlation_id"] == "abc"
    assert payload["stage"] == "test"
    assert "secret" not in path.read_text("utf-8")


def test_metrics_are_stored_without_private_content(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    metrics = MetricsStore(database)
    metrics.record(
        correlation_id="abc",
        stage="routing",
        duration_ms=12.5,
        value=0.9,
        outcome="core.timer",
        dimensions=MetricDimensions(
            interaction=InteractionMode.TYPED,
            path=RequestPath.DETERMINISTIC,
        ),
    )
    value = metrics.recent()[0]
    assert value["duration_ms"] == 12.5
    assert value["dimensions"] == {
        "interaction": "typed",
        "path": "deterministic",
    }
    assert "transcript" not in value


def test_metrics_filter_and_reject_unbounded_dimensions(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    metrics = MetricsStore(database)
    metrics.record(
        correlation_id="typed",
        stage="request.total",
        duration_ms=10,
        dimensions={"interaction": "typed"},
    )
    metrics.record(
        correlation_id="voice",
        stage="request.total",
        duration_ms=20,
        dimensions={"interaction": "voice"},
    )

    assert [
        item["correlation_id"] for item in metrics.query(dimensions={"interaction": "voice"})
    ] == ["voice"]
    with pytest.raises(ValueError, match="Unsupported metric dimension"):
        metrics.record(
            correlation_id="private",
            stage="request.total",
            dimensions={"transcript": "do not store this"},
        )


def test_stage_summary_keeps_latency_and_failure_counts_separate(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    metrics = MetricsStore(database)
    for outcome, duration in (("completed", 10), ("failed", 20), ("cancelled", 30)):
        metrics.record(
            correlation_id=outcome,
            stage="execution",
            duration_ms=duration,
            outcome=outcome,
            dimensions=MetricDimensions(interaction=InteractionMode.TYPED),
        )

    summary = metrics.stage_summary(
        since=datetime.now(UTC) - timedelta(minutes=1),
        stage="execution",
        dimensions={"interaction": "typed"},
    )

    assert summary["count"] == 3
    assert summary["p50"] == 20
    assert summary["p95"] == 30
    assert summary["failed"] == 2
    assert summary["error_rate"] == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_user_facing_trace_records_safe_decisions_actions_and_results(settings):
    container = build_container(settings)

    _, outcome = await container.orchestrator.handle_text("calculate 8 times 9")

    assert outcome.call is not None
    events = container.events.history(outcome.call.correlation_id)
    phases = [event.trace.phase.value for event in events if event.trace is not None]
    assert phases == ["understanding", "decision", "permission", "action", "result"]
    assert all("expression" not in event.trace.detail for event in events if event.trace)
