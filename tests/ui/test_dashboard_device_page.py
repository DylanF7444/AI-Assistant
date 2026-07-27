from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.bootstrap import build_container
from assistant.dashboard.contracts import (
    DashboardCard,
    DashboardLayout,
    MetricPresentation,
    MetricState,
)
from assistant.storage.database import SettingsStore
from assistant.ui.desktop.app import run_desktop


@pytest.mark.ui
def test_device_card_offers_permission_and_refreshes_after_grant(qtbot, settings, monkeypatch):
    from PySide6.QtCore import Qt
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
    provider, _definition = container.dashboard.registry.resolve("device.cpu")
    provider.adapter = _FakeDeviceAdapter()
    card = DashboardCard(
        card_id=__import__("uuid").uuid4(),
        metric_id="device.cpu",
        presentation=MetricPresentation.PERCENTAGE,
        refresh_seconds=30,
        position=0,
    )
    container.dashboard.save_layout(DashboardLayout(cards=(card,)))
    SettingsStore(container.database).set("first_run_complete", True)

    assert run_desktop(container) == 0
    window = captured_windows[-1]
    qtbot.addWidget(window)
    window.open_dashboard_from_tray()
    qtbot.waitUntil(lambda: not window.dashboard_page.refreshing, timeout=5000)

    widget = window.dashboard_page._cards[card.card_id]
    assert widget.property("state") == MetricState.PERMISSION_REQUIRED.value
    assert widget.permission_button.isVisible()

    choices = iter(("approve", "session"))
    monkeypatch.setattr(window, "_voice_prompt_choice", lambda **kwargs: next(choices))
    qtbot.mouseClick(
        widget.permission_button,
        Qt.MouseButton.LeftButton,
    )
    qtbot.waitUntil(lambda: not window.dashboard_page.refreshing, timeout=5000)

    assert widget.property("state") == MetricState.READY.value
    assert widget.value.text() == "25.0 %"
    assert not widget.permission_button.isVisible()
    window.shutdown()
    window.hide()


class _FakeDeviceAdapter:
    async def performance_status(self) -> dict:
        return {
            "cpu_percent": 25,
            "memory": {
                "percent_used": 60,
                "available_bytes": 8 * 1024**3,
                "total_bytes": 20 * 1024**3,
            },
        }

    async def storage_status(self, drive: str | None = None) -> list[dict]:
        return []

    async def battery_status(self) -> dict:
        return {"battery_present": False, "percent": None}
