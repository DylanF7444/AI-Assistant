from __future__ import annotations

from PySide6.QtWidgets import QWidget

from assistant.integrations.google import GoogleConnectionService
from assistant.ui.desktop.oauth_connection import (
    OAuthConnectionDialog,
    OAuthConnectionDialogConfig,
)

_CONFIG = OAuthConnectionDialogConfig(
    provider_name="Google",
    window_title="Google Drive Connection",
    description=(
        "Sign in with Google to connect Drive. Google opens in your system browser "
        "and asks you to approve read-only Drive metadata access. The same encrypted "
        "Google account can also power Calendar and Gmail; this extension asks only for "
        "its additional Drive scope. Tokens are never given to extension package code or "
        "the language model. If this Google project requires an OAuth client secret, "
        "Local Assistant stores it encrypted after you enter it once."
    ),
    object_prefix="googleDrive",
    client_id_placeholder="Desktop OAuth client ID from Google Cloud Console",
    connect_label="Sign in with Google",
    test_busy_message="Testing Google Drive…",
    disconnect_title="Disconnect Google Drive",
    disconnect_question=(
        "Revoke the shared Google authorization and remove local tokens for Calendar, "
        "Gmail, and Drive?"
    ),
    disconnect_busy_message="Revoking Google Drive authorization…",
)


class GoogleDriveConnectionDialog(OAuthConnectionDialog):
    def __init__(
        self,
        service: GoogleConnectionService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(service, _CONFIG, parent)
