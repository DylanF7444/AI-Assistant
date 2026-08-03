from __future__ import annotations

from PySide6.QtWidgets import QWidget

from assistant.integrations.notion import NotionConnectionService
from assistant.ui.desktop.api_token_connection import (
    ApiTokenConnectionDialog,
    ApiTokenConnectionDialogConfig,
)

_CONFIG = ApiTokenConnectionDialogConfig(
    provider_name="Notion",
    window_title="Notion Connection",
    description=(
        "Enter a Notion internal integration token or personal access token with read-only "
        "capabilities. Share the pages or data sources you want the integration to read in "
        "Notion. The token is encrypted for this Windows account and never given to extension "
        "package code or the language model."
    ),
    object_prefix="notion",
    token_label="Notion token",
    token_placeholder="ntn_…",
    connect_label="Connect Notion",
    test_busy_message="Testing Notion…",
    disconnect_title="Disconnect Notion",
    disconnect_question="Remove the encrypted Notion token from this Windows account?",
    disconnect_busy_message="Disconnecting Notion…",
)


class NotionConnectionDialog(ApiTokenConnectionDialog):
    def __init__(self, service: NotionConnectionService, parent: QWidget | None = None) -> None:
        super().__init__(service, _CONFIG, parent)
