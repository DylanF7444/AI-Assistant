from __future__ import annotations

import json
import logging

import pytest

from assistant.bootstrap import build_container
from assistant.logging import configure_logging
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
    )
    value = metrics.recent()[0]
    assert value["duration_ms"] == 12.5
    assert "transcript" not in value


@pytest.mark.asyncio
async def test_user_facing_trace_records_safe_decisions_actions_and_results(settings):
    container = build_container(settings)

    _, outcome = await container.orchestrator.handle_text("calculate 8 times 9")

    assert outcome.call is not None
    events = container.events.history(outcome.call.correlation_id)
    phases = [event.trace.phase.value for event in events if event.trace is not None]
    assert phases == ["understanding", "decision", "permission", "action", "result"]
    assert all("expression" not in event.trace.detail for event in events if event.trace)
