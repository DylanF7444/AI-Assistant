from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.ui.desktop.anthropic_connection import AnthropicConnectionDialog
from assistant.ui.desktop.openai_connection import OpenAIConnectionDialog


class FakeConnectionService:
    def status(self):
        return SimpleNamespace(
            connected=False,
            authenticated=False,
            usable=False,
            account=None,
            healthy=None,
            message="No API key is connected.",
        )


@pytest.mark.ui
@pytest.mark.parametrize(
    ("dialog_type", "window_title", "token_label"),
    (
        (OpenAIConnectionDialog, "OpenAI Connection", "OpenAI API key"),
        (AnthropicConnectionDialog, "Anthropic Connection", "Anthropic API key"),
    ),
)
def test_cloud_connection_dialogs_explain_and_protect_api_keys(
    qtbot,
    dialog_type,
    window_title,
    token_label,
):
    dialog = dialog_type(FakeConnectionService())
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == window_title
    assert dialog.token_input.echoMode() != dialog.token_input.EchoMode.Normal
    assert dialog.config.token_label == token_label
    assert "encrypted" in dialog.config.description.casefold()
