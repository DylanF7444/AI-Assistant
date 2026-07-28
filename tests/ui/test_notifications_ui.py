from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.bootstrap import build_container
from assistant.scheduling.notifications import NotificationMessage
from assistant.storage.database import SettingsStore
from assistant.ui.desktop.app import run_desktop
from assistant.ui.desktop.tabs import TAB_LABELS


@pytest.mark.ui
def test_notification_inbox_badge_drawer_and_read_state(
    qtbot,
    settings,
    monkeypatch,
):
    from PySide6.QtWidgets import QApplication, QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    captured = []
    existing = {id(widget) for widget in QApplication.topLevelWidgets()}

    def capture_window(self):
        captured.extend(
            widget
            for widget in QApplication.topLevelWidgets()
            if widget.objectName() == "localAssistantWindow" and id(widget) not in existing
        )
        return 0

    monkeypatch.setattr(QApplication, "exec", capture_window)
    container = build_container(settings)
    SettingsStore(container.database).set("first_run_complete", True)
    assert run_desktop(container) == 0
    window = captured[-1]
    qtbot.addWidget(window)
    qtbot.addWidget(window.result_window)

    container.notification_center.publish(
        NotificationMessage(
            title="Daily report ready",
            message="Your morning report is ready to review.",
            severity="success",
            category="report",
        )
    )

    assert window.notification_table.rowCount() == 1
    assert window.tabs.tabText(TAB_LABELS.index("Inbox")) == "Inbox (1)"
    assert window.compact_panel.inbox_button.text() == "Inbox (1)"

    window._toggle_notification_drawer()
    assert window.notification_drawer.isVisible()
    assert window.notification_drawer.items.count() == 1

    window.notification_table.selectRow(0)
    window._open_selected_notification()
    assert container.notification_center.unread_count() == 0
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Inbox"

    window._dismiss_selected_notification()
    assert window.notification_table.rowCount() == 0

    run_id = uuid4()
    schedule_id = uuid4()
    report_notification = NotificationMessage(
        title="Report ready",
        message="Your morning report is ready to review.",
        severity="success",
        category="report",
        schedule_id=schedule_id,
        run_id=run_id,
    )
    container.notification_center.publish(report_notification)
    monkeypatch.setattr(
        container.schedule_store,
        "get_run",
        lambda requested: SimpleNamespace(
            run_id=requested,
            schedule_id=schedule_id,
            result={
                "summary": "Three tasks completed.",
                "display_text": "Daily report\n\nThree tasks completed; one needs review.",
            },
            completed_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
            started_at=None,
            scheduled_for=datetime(2026, 7, 28, 12, tzinfo=UTC),
            state=SimpleNamespace(value="succeeded"),
            result_status="completed",
        ),
    )
    monkeypatch.setattr(
        container.schedule_store,
        "list",
        lambda *args, **kwargs: (SimpleNamespace(schedule_id=schedule_id, name="Daily report"),),
    )

    window._open_notification(str(report_notification.notification_id))
    assert window.result_window.isVisible()
    assert window.result_window.report_title.text() == "Daily report"
    assert "Three tasks completed" in window.result_window.report_body.toPlainText()

    window.shutdown()
    window.hide()


@pytest.mark.ui
def test_foreground_banner_opens_the_matching_notification(qtbot):
    from assistant.ui.desktop.notification_widgets import NotificationBanner

    banner = NotificationBanner()
    qtbot.addWidget(banner)
    message = NotificationMessage(title="Completed", message="The report is ready.")

    with qtbot.waitSignal(banner.open_requested, timeout=1000) as signal:
        banner.show_notification(message)
        banner.open_button.click()

    assert signal.args == [str(message.notification_id)]
    assert not banner.isVisible()
