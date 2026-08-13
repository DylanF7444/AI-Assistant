from __future__ import annotations

from PySide6.QtWidgets import QWidget

from assistant.integrations.openai import OpenAIConnectionService
from assistant.ui.desktop.api_token_connection import (
    ApiTokenConnectionDialog,
    ApiTokenConnectionDialogConfig,
)

_CONFIG = ApiTokenConnectionDialogConfig(
    provider_name="OpenAI",
    window_title="OpenAI Connection",
    description=(
        "Enter an OpenAI API key. It is encrypted for this Windows account and used only by "
        "the reviewed OpenAI extension; it is never given to package code, transcripts, or "
        "the language model."
    ),
    object_prefix="openai",
    token_label="OpenAI API key",
    token_placeholder="sk-…",
    connect_label="Connect OpenAI",
    test_busy_message="Testing OpenAI…",
    disconnect_title="Disconnect OpenAI",
    disconnect_question="Remove the encrypted OpenAI API key from this Windows account?",
    disconnect_busy_message="Disconnecting OpenAI…",
)


class OpenAIConnectionDialog(ApiTokenConnectionDialog):
    def __init__(self, service: OpenAIConnectionService, parent: QWidget | None = None) -> None:
        super().__init__(service, _CONFIG, parent)
