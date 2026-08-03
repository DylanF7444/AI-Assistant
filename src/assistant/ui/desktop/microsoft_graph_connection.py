from __future__ import annotations

from PySide6.QtWidgets import QWidget

from assistant.integrations.microsoft_graph import MicrosoftGraphConnectionService
from assistant.ui.desktop.oauth_connection import (
    OAuthConnectionDialog,
    OAuthConnectionDialogConfig,
)

_CONFIG = OAuthConnectionDialogConfig(
    provider_name="Microsoft",
    window_title="Microsoft Account Connection",
    description=(
        "Sign in with Microsoft to connect Outlook Mail, Outlook Calendar, or OneDrive. "
        "Microsoft opens in your system browser and asks for the read-only access required "
        "by the selected extension. Tokens are encrypted for this Windows account and are "
        "never given to extension package code or the language model. A Microsoft Entra "
        "desktop/public client ID is required; expand OAuth settings if it is not already "
        "configured."
    ),
    object_prefix="microsoftGraph",
    client_id_placeholder="Microsoft Entra desktop/public client ID",
    connect_label="Sign in with Microsoft",
    test_busy_message="Testing Microsoft Graph…",
    disconnect_title="Disconnect Microsoft Account",
    disconnect_question="Remove the encrypted local Microsoft authorization tokens?",
    disconnect_busy_message="Disconnecting Microsoft account…",
)


class MicrosoftGraphConnectionDialog(OAuthConnectionDialog):
    def __init__(
        self,
        service: MicrosoftGraphConnectionService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(service, _CONFIG, parent)
