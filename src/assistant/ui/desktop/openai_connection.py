from __future__ import annotations

from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from assistant.integrations.openai import OpenAIConnectionService
from assistant.integrations.web import CodexSetupService, CodexSetupStatus
from assistant.ui.desktop.api_token_connection import (
    ApiTokenConnectionDialog,
    ApiTokenConnectionDialogConfig,
)
from assistant.ui.desktop.workers import AsyncWorker

_CONFIG = ApiTokenConnectionDialogConfig(
    provider_name="OpenAI",
    window_title="OpenAI & Codex Setup",
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
    def __init__(
        self,
        service: OpenAIConnectionService,
        parent: QWidget | None = None,
        *,
        codex_service: Any | None = None,
    ) -> None:
        super().__init__(service, _CONFIG, parent)
        self.codex_service = (
            codex_service or getattr(service, "codex_setup_service", None) or CodexSetupService()
        )
        self._codex_busy = False

        self.codex_setup_group = QGroupBox("Codex CLI (separate ChatGPT sign-in)")
        self.codex_setup_group.setObjectName("codexSetupGroup")
        codex_layout = QVBoxLayout(self.codex_setup_group)
        explanation = QLabel(
            "Codex does not use the OpenAI API key above. The assistant runs the local "
            "Codex CLI, which must be installed and signed in separately."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("pageSubtitle")
        codex_layout.addWidget(explanation)
        self.codex_status_label = QLabel()
        self.codex_status_label.setWordWrap(True)
        self.codex_status_label.setObjectName("codexStatus")
        codex_layout.addWidget(self.codex_status_label)

        actions = QHBoxLayout()
        self.codex_install_button = QPushButton("Install / Update Codex")
        self.codex_install_button.setObjectName("codexInstallButton")
        self.codex_install_button.clicked.connect(self._open_codex_install_guide)
        self.codex_login_button = QPushButton("Sign in with ChatGPT")
        self.codex_login_button.setObjectName("codexLoginButton")
        self.codex_login_button.clicked.connect(self._sign_in_codex)
        self.codex_check_button = QPushButton("Check Codex Setup")
        self.codex_check_button.setObjectName("codexCheckButton")
        self.codex_check_button.clicked.connect(self._check_codex)
        actions.addWidget(self.codex_install_button)
        actions.addWidget(self.codex_login_button)
        actions.addWidget(self.codex_check_button)
        codex_layout.addLayout(actions)

        layout = self.layout()
        if isinstance(layout, QVBoxLayout):
            layout.insertWidget(max(0, layout.count() - 1), self.codex_setup_group)
        self._render_codex_status()

    def _render_codex_status(self, status: CodexSetupStatus | None = None) -> None:
        status = status or self.codex_service.status()
        self.codex_status_label.setText(status.message)
        self.codex_login_button.setEnabled(status.installed and not self._codex_busy)
        self.codex_check_button.setEnabled(status.installed and not self._codex_busy)
        self.codex_install_button.setEnabled(not self._codex_busy)

    def _open_codex_install_guide(self) -> None:
        if not QDesktopServices.openUrl(QUrl(self.codex_service.install_url)):
            self.codex_status_label.setText("The Codex install guide could not be opened.")

    def _sign_in_codex(self) -> None:
        self._set_codex_busy(
            "Opening Codex sign-in in your browser. Complete the browser flow to continue…"
        )
        self._start_codex_worker(AsyncWorker(self.codex_service.sign_in()))

    def _check_codex(self) -> None:
        self._set_codex_busy("Checking Codex installation and sign-in…")
        self._start_codex_worker(AsyncWorker(self.codex_service.check()))

    def _start_codex_worker(self, worker: AsyncWorker) -> None:
        worker.complete.connect(self._codex_operation_complete)
        worker.failed.connect(self._codex_operation_failed)
        worker.finished.connect(lambda: self._worker_finished(worker))
        self._workers.append(worker)
        worker.start()

    def _codex_operation_complete(self, status: CodexSetupStatus) -> None:
        self._codex_busy = False
        self._render_codex_status(status)
        self.connection_changed.emit()

    def _codex_operation_failed(self, error: str) -> None:
        self._codex_busy = False
        self._render_codex_status()
        self.codex_status_label.setText(f"Codex setup failed: {error}")

    def _set_codex_busy(self, message: str) -> None:
        self._codex_busy = True
        self.codex_status_label.setText(message)
        self.codex_install_button.setEnabled(False)
        self.codex_login_button.setEnabled(False)
        self.codex_check_button.setEnabled(False)
