from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.bootstrap import build_container
from assistant.storage.database import SettingsStore
from assistant.ui.desktop.app import run_desktop
from assistant.ui.desktop.state import DesktopWindowState


@pytest.mark.ui
def test_dashboard_is_a_persisted_primary_screen(qtbot, settings, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    captured_windows = []
    existing_windows = {id(widget) for widget in QApplication.topLevelWidgets()}

    def capture_window(self):
        captured_windows.extend(
            widget
            for widget in QApplication.topLevelWidgets()
            if widget.objectName() == "localAssistantWindow" and id(widget) not in existing_windows
        )
        return 0

    monkeypatch.setattr(QApplication, "exec", capture_window)
    container = build_container(settings)
    store = SettingsStore(container.database)
    store.set("first_run_complete", True)

    assert run_desktop(container) == 0
    window = captured_windows[-1]
    qtbot.addWidget(window)

    window.open_dashboard_from_tray()
    assert window._window_mode == "full"
    assert window.tabs.currentWidget() is window.dashboard_page
    qtbot.waitUntil(lambda: not window.dashboard_page.refreshing, timeout=5000)
    assert len(window.dashboard_page._cards) == 8
    assert any(
        widget.title.text() == "Tasks" for widget in window.dashboard_page._cards.values()
    )
    assert window.dashboard_page.last_refresh.text().startswith("Updated")
    window._refresh_diagnostics()
    assert "Dashboard" in window.diagnostic_text.toPlainText()
    assert "Refresh attempts: 8" in window.diagnostic_text.toPlainText()

    persisted = DesktopWindowState.model_validate(store.get("desktop_window_state", {}))
    assert persisted.primary_screen == "dashboard"

    window.tabs.setCurrentIndex(window.tabs.indexOf(window.permission_table.parentWidget()))
    window.hide()
    window.show_from_tray()
    assert window.tabs.currentWidget() is window.dashboard_page

    window.open_assistant_from_tray()
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Assistant"
    persisted = DesktopWindowState.model_validate(store.get("desktop_window_state", {}))
    assert persisted.primary_screen == "assistant"

    window.shutdown()
    window.hide()
