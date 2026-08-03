from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from assistant.ui.desktop.slack_connection import SlackConnectionDialog


class FakeConnectionService:
    def __init__(self):
        self.connected = False
        self.account = None
        self.tokens = []

    def status(self):
        return SimpleNamespace(
            connected=self.connected,
            authenticated=self.connected,
            account=self.account,
            healthy=None,
            message="Connected." if self.connected else "Not connected.",
        )

    async def connect(self, token):
        self.tokens.append(token)
        self.connected = True
        self.account = "Example Workspace"
        return SimpleNamespace(
            connected=True,
            authenticated=True,
            account=self.account,
            healthy=True,
            message="Slack connected.",
        )

    async def test(self):
        return SimpleNamespace(
            connected=True,
            authenticated=True,
            account=self.account,
            healthy=True,
            message="Slack connection is healthy.",
        )

    async def disconnect(self):
        self.connected = False
        self.account = None
        return self.status()


@pytest.mark.ui
def test_api_token_connection_dialog_keeps_token_input_and_lifecycle(qtbot, monkeypatch):
    service = FakeConnectionService()
    dialog = SlackConnectionDialog(service)
    qtbot.addWidget(dialog)
    dialog.token_input.setText("secret-token")

    with qtbot.waitSignal(dialog.connection_changed, timeout=2000):
        qtbot.mouseClick(dialog.connect_button, Qt.MouseButton.LeftButton)

    assert service.tokens == ["secret-token"]
    assert "Example Workspace" in dialog.status_label.text()
    assert dialog.token_input.echoMode() != dialog.token_input.EchoMode.Normal

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    with qtbot.waitSignal(dialog.connection_changed, timeout=2000):
        qtbot.mouseClick(dialog.disconnect_button, Qt.MouseButton.LeftButton)
    assert service.connected is False
