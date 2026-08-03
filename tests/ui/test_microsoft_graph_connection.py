from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from assistant.ui.desktop.microsoft_graph_connection import MicrosoftGraphConnectionDialog


class FakeConnectionService:
    def __init__(self):
        self.default_client_id = "entra-desktop-client"
        self.connected = False
        self.account = None
        self.connect_calls = []

    def status(self):
        return SimpleNamespace(
            connected=self.connected,
            authenticated=self.connected,
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
            authenticated=True,
            account=self.account,
            healthy=True,
            message="Microsoft account connected.",
        )

    async def test(self):
        return SimpleNamespace(
            connected=True,
            authenticated=True,
            account=self.account,
            healthy=True,
            message="Microsoft Graph connection is healthy.",
        )

    async def disconnect(self):
        self.connected = False
        self.account = None
        return self.status()


@pytest.mark.ui
def test_microsoft_graph_connection_dialog_supports_account_lifecycle(qtbot, monkeypatch):
    service = FakeConnectionService()
    dialog = MicrosoftGraphConnectionDialog(service)
    qtbot.addWidget(dialog)

    dialog.advanced_toggle.setChecked(True)
    with qtbot.waitSignal(dialog.connection_changed, timeout=2000):
        qtbot.mouseClick(dialog.connect_button, Qt.MouseButton.LeftButton)

    assert service.connect_calls == [("entra-desktop-client", None)]
    assert "person@example.com" in dialog.status_label.text()
    assert dialog.test_button.isEnabled()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    with qtbot.waitSignal(dialog.connection_changed, timeout=2000):
        qtbot.mouseClick(dialog.disconnect_button, Qt.MouseButton.LeftButton)

    assert service.connected is False
