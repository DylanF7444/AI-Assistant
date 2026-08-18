from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.ui.desktop.anthropic_connection import AnthropicConnectionDialog
from assistant.ui.desktop.openai_connection import OpenAIConnectionDialog


class FakeCodexService:
    install_url = "https://learn.chatgpt.com/docs/codex/cli"

    def status(self):
        return SimpleNamespace(
            installed=False,
            authenticated=False,
            message="Codex CLI is not installed.",
        )


class FakeConnectionService:
    codex_setup_service = FakeCodexService()

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
        (OpenAIConnectionDialog, "OpenAI & Codex Setup", "OpenAI API key"),
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


@pytest.mark.ui
def test_openai_dialog_exposes_separate_codex_setup(qtbot):
    dialog = OpenAIConnectionDialog(FakeConnectionService())
    qtbot.addWidget(dialog)

    assert dialog.findChild(type(dialog.codex_status_label), "codexStatus") is not None
    assert dialog.codex_install_button.text() == "Install / Update Codex"
    assert dialog.codex_login_button.text() == "Sign in with ChatGPT"
    assert dialog.codex_check_button.text() == "Check Codex Setup"
    assert not dialog.codex_login_button.isEnabled()
    assert "separate" in dialog.codex_setup_group.title().casefold()
