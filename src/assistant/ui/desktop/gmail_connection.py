from __future__ import annotations

from PySide6.QtWidgets import QWidget

from assistant.integrations.gmail import GmailConnectionService
from assistant.ui.desktop.oauth_connection import (
    OAuthConnectionDialog,
    OAuthConnectionDialogConfig,
)

_CONFIG = OAuthConnectionDialogConfig(
    provider_name="Google",
    window_title="Gmail Connection",
    description=(
        "Sign in with Google to connect Gmail. Google opens in your system browser and "
        "asks you to approve access to read, organize, draft, and send email. Tokens are "
        "encrypted for this Windows account and are never given to extension package code "
        "or the language model. If this Google project requires an OAuth client secret, "
        "Local Assistant stores it encrypted after you enter it once."
    ),
    object_prefix="gmail",
    client_id_placeholder="Desktop OAuth client ID from Google Cloud Console",
    connect_label="Sign in with Google",
    test_busy_message="Testing Gmail…",
    disconnect_title="Disconnect Gmail",
    disconnect_question="Revoke Google authorization and remove the encrypted local tokens?",
    disconnect_busy_message="Revoking Google authorization…",
)


class GmailConnectionDialog(OAuthConnectionDialog):
    def __init__(
        self,
        service: GmailConnectionService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(service, _CONFIG, parent)
