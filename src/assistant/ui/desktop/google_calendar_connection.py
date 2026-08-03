from __future__ import annotations

from PySide6.QtWidgets import QWidget

from assistant.integrations.google_calendar import GoogleCalendarConnectionService
from assistant.ui.desktop.oauth_connection import (
    OAuthConnectionDialog,
    OAuthConnectionDialogConfig,
)

_CONFIG = OAuthConnectionDialogConfig(
    provider_name="Google",
    window_title="Google Calendar Connection",
    description=(
        "Sign in with Google to connect Calendar. Google opens in your system browser "
        "and asks you to approve calendar access. Tokens are encrypted for this Windows "
        "account and are never given to extension package code or the language model. "
        "If this Google project requires an OAuth client secret, Local Assistant stores "
        "it encrypted after you enter it once."
    ),
    object_prefix="googleCalendar",
    client_id_placeholder="Desktop OAuth client ID from Google Cloud Console",
    connect_label="Sign in with Google",
    test_busy_message="Testing Google Calendar…",
    disconnect_title="Disconnect Google Calendar",
    disconnect_question="Revoke Google authorization and remove the encrypted local tokens?",
    disconnect_busy_message="Revoking Google authorization…",
)


class GoogleCalendarConnectionDialog(OAuthConnectionDialog):
    def __init__(
        self,
        service: GoogleCalendarConnectionService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(service, _CONFIG, parent)
