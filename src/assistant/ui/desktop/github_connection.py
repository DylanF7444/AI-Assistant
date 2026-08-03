from __future__ import annotations

from PySide6.QtWidgets import QWidget

from assistant.integrations.github import GitHubConnectionService
from assistant.ui.desktop.api_token_connection import (
    ApiTokenConnectionDialog,
    ApiTokenConnectionDialogConfig,
)

_CONFIG = ApiTokenConnectionDialogConfig(
    provider_name="GitHub",
    window_title="GitHub Connection",
    description=(
        "Enter a GitHub fine-grained personal access token with read-only Metadata and issue "
        "access for the repositories you intend to inspect. The token is encrypted for this "
        "Windows account and never given to extension package code or the language model. A "
        "narrow GitHub App authorization flow is planned for a later release."
    ),
    object_prefix="github",
    token_label="GitHub token",
    token_placeholder="github_pat_…",
    connect_label="Connect GitHub",
    test_busy_message="Testing GitHub…",
    disconnect_title="Disconnect GitHub",
    disconnect_question="Remove the encrypted GitHub token from this Windows account?",
    disconnect_busy_message="Disconnecting GitHub…",
)


class GitHubConnectionDialog(ApiTokenConnectionDialog):
    def __init__(self, service: GitHubConnectionService, parent: QWidget | None = None) -> None:
        super().__init__(service, _CONFIG, parent)
