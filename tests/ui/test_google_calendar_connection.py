from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from assistant.ui.desktop.google_calendar_connection import (
    GoogleCalendarConnectionDialog,
)


class FakeConnectionService:
    def __init__(self):
        self.default_client_id = "bundled-desktop-client"
        self.connected = False
        self.account = None
        self.connect_calls = []

    def status(self):
        return SimpleNamespace(
            connected=self.connected,
            account=self.account,
            healthy=None,
            message="Connected." if self.connected else "Not connected.",
        )

    async def connect(self, client_id=None, *, client_secret=None):
        self.connect_calls.append((client_id, client_secret))
        self.connected = True
        self.account = "person@example.com"
        return SimpleNamespace(
            connected=True,
            account=self.account,
            healthy=True,
            message="Google Calendar account connected.",
        )

    async def test(self):
        return SimpleNamespace(
            connected=True,
            account=self.account,
            healthy=True,
            message="Google Calendar connection is healthy.",
        )

    async def disconnect(self):
        self.connected = False
        self.account = None
        return self.status()


class FailingConnectionService(FakeConnectionService):
    async def connect(self, client_id=None, *, client_secret=None):
        self.connect_calls.append((client_id, client_secret))
        raise RuntimeError("Google rejected the authorization code.")


@pytest.mark.ui
def test_google_calendar_connection_dialog_connects_tests_and_disconnects(qtbot, monkeypatch):
    service = FakeConnectionService()
    dialog = GoogleCalendarConnectionDialog(service)
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.connection_changed, timeout=2000):
        qtbot.mouseClick(dialog.connect_button, Qt.MouseButton.LeftButton)

    assert service.connect_calls == [(None, None)]
    assert "person@example.com" in dialog.status_label.text()
    assert dialog.test_button.isEnabled()

    qtbot.mouseClick(dialog.test_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(
        lambda: "healthy" in dialog.status_label.text().casefold(),
        timeout=2000,
    )

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    with qtbot.waitSignal(dialog.connection_changed, timeout=2000):
        qtbot.mouseClick(dialog.disconnect_button, Qt.MouseButton.LeftButton)

    assert service.connected is False
    assert not dialog.test_button.isEnabled()


@pytest.mark.ui
def test_google_calendar_connection_dialog_supports_advanced_oauth_override(qtbot):
    service = FakeConnectionService()
    dialog = GoogleCalendarConnectionDialog(service)
    qtbot.addWidget(dialog)

    assert dialog.advanced_widget.isHidden()
    dialog.advanced_toggle.setChecked(True)
    assert dialog.client_id_input.isEnabled()
    assert dialog.client_secret_input.isEnabled()
    dialog.client_id_input.setText("different-desktop-client")
    dialog.client_secret_input.setText("optional-secret")

    with qtbot.waitSignal(dialog.connection_changed, timeout=2000):
        qtbot.mouseClick(dialog.connect_button, Qt.MouseButton.LeftButton)

    assert service.connect_calls == [
        ("different-desktop-client", "optional-secret")
    ]


@pytest.mark.ui
def test_google_calendar_connection_dialog_keeps_tests_disabled_after_oauth_failure(qtbot):
    service = FailingConnectionService()
    dialog = GoogleCalendarConnectionDialog(service)
    qtbot.addWidget(dialog)

    qtbot.mouseClick(dialog.connect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(
        lambda: "Google rejected the authorization code" in dialog.status_label.text(),
        timeout=2000,
    )

    assert service.connected is False
    assert not dialog.test_button.isEnabled()
    assert dialog.connect_button.isEnabled()
