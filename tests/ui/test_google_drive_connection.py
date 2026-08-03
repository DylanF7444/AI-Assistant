from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from assistant.ui.desktop.google_drive_connection import GoogleDriveConnectionDialog


class FakeConnectionService:
    def __init__(self):
        self.default_client_id = "bundled-desktop-client"
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
            message="Google Drive account connected.",
        )

    async def test(self):
        return SimpleNamespace(
            connected=True,
            authenticated=True,
            account=self.account,
            healthy=True,
            message="Google Drive connection is healthy.",
        )

    async def disconnect(self):
        self.connected = False
        self.account = None
        return self.status()


@pytest.mark.ui
def test_google_drive_connection_dialog_supports_account_lifecycle(qtbot, monkeypatch):
    service = FakeConnectionService()
    dialog = GoogleDriveConnectionDialog(service)
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.connection_changed, timeout=2000):
        qtbot.mouseClick(dialog.connect_button, Qt.MouseButton.LeftButton)

    assert service.connect_calls == [(None, None)]
    assert "person@example.com" in dialog.status_label.text()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    with qtbot.waitSignal(dialog.connection_changed, timeout=2000):
        qtbot.mouseClick(dialog.disconnect_button, Qt.MouseButton.LeftButton)

    assert service.connected is False
