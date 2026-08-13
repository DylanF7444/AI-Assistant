from __future__ import annotations

from PySide6.QtWidgets import QWidget

from assistant.integrations.anthropic import AnthropicConnectionService
from assistant.ui.desktop.api_token_connection import (
    ApiTokenConnectionDialog,
    ApiTokenConnectionDialogConfig,
)

_CONFIG = ApiTokenConnectionDialogConfig(
    provider_name="Anthropic",
    window_title="Anthropic Connection",
    description=(
        "Enter an Anthropic API key. It is encrypted for this Windows account and used only by "
        "the reviewed Anthropic extension; it is never given to package code, transcripts, or "
        "the language model."
    ),
    object_prefix="anthropic",
    token_label="Anthropic API key",
    token_placeholder="sk-ant-…",
    connect_label="Connect Anthropic",
    test_busy_message="Testing Anthropic…",
    disconnect_title="Disconnect Anthropic",
    disconnect_question="Remove the encrypted Anthropic API key from this Windows account?",
    disconnect_busy_message="Disconnecting Anthropic…",
)


class AnthropicConnectionDialog(ApiTokenConnectionDialog):
    def __init__(
        self,
        service: AnthropicConnectionService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(service, _CONFIG, parent)
