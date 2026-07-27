from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.dashboard.contracts import (
    DashboardCard,
    MetricDefinition,
    MetricPoint,
    MetricPresentation,
    MetricSnapshot,
    MetricState,
    MetricValueKind,
)
from assistant.ui.desktop.dashboard import DashboardMetricCard


@pytest.mark.ui
def test_dashboard_progress_presentation_clamps_numeric_value(qtbot):
    card = _card(MetricPresentation.PROGRESS)
    widget = DashboardMetricCard(card, _definition())
    qtbot.addWidget(widget)
    widget.show()

    widget.apply_snapshot(_snapshot(value=125))

    assert widget.progress.isVisible()
    assert widget.progress.value() == 100
    assert not widget.sparkline.isVisible()


@pytest.mark.ui
def test_dashboard_trend_presentation_renders_two_or_more_points(qtbot):
    card = _card(MetricPresentation.TREND)
    widget = DashboardMetricCard(card, _definition())
    qtbot.addWidget(widget)
    widget.show()
    now = datetime.now(UTC)

    widget.apply_snapshot(
        _snapshot(
            value=22,
            trend=(
                MetricPoint(timestamp=now - timedelta(minutes=1), value=20),
                MetricPoint(timestamp=now, value=22),
            ),
        )
    )

    assert widget.sparkline.isVisible()
    assert widget.sparkline._points[-1].value == 22
    assert not widget.progress.isVisible()


def _card(presentation: MetricPresentation) -> DashboardCard:
    return DashboardCard(
        card_id=uuid4(),
        metric_id="test.value",
        presentation=presentation,
        refresh_seconds=60,
        position=0,
    )


def _definition() -> MetricDefinition:
    return MetricDefinition(
        metric_id="test.value",
        provider_id="test",
        display_name="Test value",
        description="A presentation test metric.",
        source_label="Test",
        value_kind=MetricValueKind.NUMBER,
        supported_presentations=(
            MetricPresentation.PROGRESS,
            MetricPresentation.TREND,
        ),
        default_presentation=MetricPresentation.PROGRESS,
    )


def _snapshot(
    *,
    value: float,
    trend: tuple[MetricPoint, ...] = (),
) -> MetricSnapshot:
    return MetricSnapshot(
        metric_id="test.value",
        collected_at=datetime.now(UTC),
        state=MetricState.READY,
        value=value,
        trend=trend,
    )
