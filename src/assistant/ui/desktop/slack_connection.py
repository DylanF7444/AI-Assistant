from __future__ import annotations

from PySide6.QtWidgets import QWidget

from assistant.integrations.slack import SlackConnectionService
from assistant.ui.desktop.api_token_connection import (
    ApiTokenConnectionDialog,
    ApiTokenConnectionDialogConfig,
)

_CONFIG = ApiTokenConnectionDialogConfig(
    provider_name="Slack",
    window_title="Slack Connection",
    description=(
        "Enter a Slack user token with only the read scopes needed by this extension, "
        "such as search:read and conversation read scopes. The token is encrypted for "
        "this Windows account and never given to extension package code or the language model. "
        "Public OAuth installation is reserved for the follow-up callback-relay release."
    ),
    object_prefix="slack",
    token_label="Slack user token",
    token_placeholder="xoxp-…",
    connect_label="Connect Slack",
    test_busy_message="Testing Slack…",
    disconnect_title="Disconnect Slack",
    disconnect_question="Remove the encrypted Slack token from this Windows account?",
    disconnect_busy_message="Disconnecting Slack…",
)


class SlackConnectionDialog(ApiTokenConnectionDialog):
    def __init__(self, service: SlackConnectionService, parent: QWidget | None = None) -> None:
        super().__init__(service, _CONFIG, parent)
