from __future__ import annotations

import time
from uuid import uuid4

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.bootstrap import build_container
from assistant.dashboard.contracts import DashboardCard, DashboardLayout, MetricPresentation
from assistant.ui.desktop.controller import DesktopController
from assistant.ui.desktop.dashboard import DashboardPage


@pytest.mark.ui
def test_dashboard_timer_requests_only_due_cards_while_visible(qtbot, settings):
    page = DashboardPage(DesktopController(build_container(settings)))
    qtbot.addWidget(page)
    page.show()
    page._refresh_timer.stop()
    cards = tuple(page._cards)
    now = time.monotonic()
    page._last_refreshed_at = {card_id: now for card_id in cards}
    page._last_refreshed_at[cards[1]] = now - 61
    requested = []
    page.refresh_due_requested.connect(requested.append)

    page._request_due_refresh()

    assert requested == [(cards[1],)]
    page.hide()
    assert not page._refresh_timer.isActive()


@pytest.mark.ui
def test_partial_refresh_marks_only_selected_card_loading(qtbot, settings):
    page = DashboardPage(DesktopController(build_container(settings)))
    qtbot.addWidget(page)
    cards = tuple(page._cards)
    untouched = page._cards[cards[0]]
    selected = page._cards[cards[1]]
    untouched.setProperty("state", "ready")
    selected.setProperty("state", "ready")

    assert page.begin_refresh((cards[1],))

    assert selected.property("state") == "loading"
    assert untouched.property("state") == "ready"
    page.fail_refresh("failed")


@pytest.mark.ui
def test_dashboard_reflows_twenty_four_cards_into_four_wide_columns(
    qtbot,
    settings,
):
    controller = DesktopController(build_container(settings))
    controller.save_dashboard_layout(
        DashboardLayout(
            cards=tuple(
                DashboardCard(
                    card_id=uuid4(),
                    metric_id="assistant.status",
                    presentation=MetricPresentation.STATUS,
                    refresh_seconds=60,
                    position=index,
                )
                for index in range(24)
            )
        )
    )
    page = DashboardPage(controller)
    qtbot.addWidget(page)
    page.resize(1600, 900)
    page.show()
    page._refresh_timer.stop()
    page._reflow_cards(force=True)

    assert len(page._cards) == 24
    assert page._column_count == 4
