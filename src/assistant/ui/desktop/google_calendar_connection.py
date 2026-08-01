from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from assistant.integrations.google_calendar import GoogleCalendarConnectionService
from assistant.ui.desktop.workers import AsyncWorker


class GoogleCalendarConnectionDialog(QDialog):
    connection_changed = Signal()

    def __init__(
        self,
        service: GoogleCalendarConnectionService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._workers: list[AsyncWorker] = []
        self._busy = False
        self.setWindowTitle("Google Calendar Connection")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)

        description = QLabel(
            "Sign in with Google to connect Calendar. Google opens in your system browser "
            "and asks you to approve access. Tokens are encrypted for this Windows account "
            "and are never given to the extension package or language model."
        )
        description.setWordWrap(True)
        description.setObjectName("pageSubtitle")
        layout.addWidget(description)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("activityHint")
        layout.addWidget(self.status_label)

        self.advanced_toggle = QCheckBox("Use a different OAuth client")
        self.advanced_toggle.setObjectName("googleCalendarAdvancedToggle")
        layout.addWidget(self.advanced_toggle)

        self.advanced_widget = QWidget()
        self.advanced_widget.setObjectName("googleCalendarAdvancedSettings")
        form = QFormLayout(self.advanced_widget)
        self.client_id_input = QLineEdit()
        self.client_id_input.setObjectName("googleCalendarClientId")
        self.client_id_input.setPlaceholderText("Desktop OAuth client ID from Google Cloud Console")
        self.client_id_input.setText(getattr(service, "default_client_id", ""))
        self.client_secret_input = QLineEdit()
        self.client_secret_input.setObjectName("googleCalendarClientSecret")
        self.client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret_input.setPlaceholderText("Optional desktop client secret")
        form.addRow("OAuth client ID", self.client_id_input)
        form.addRow("OAuth client secret", self.client_secret_input)
        self.advanced_widget.setVisible(False)
        self.advanced_toggle.toggled.connect(self._advanced_toggled)
        layout.addWidget(self.advanced_widget)

        self.connect_button = QPushButton("Sign in with Google")
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.clicked.connect(self._connect)
        self.test_button = QPushButton("Test Connection")
        self.test_button.clicked.connect(self._test)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setObjectName("dangerButton")
        self.disconnect_button.clicked.connect(self._disconnect)
        layout.addWidget(self.connect_button)
        layout.addWidget(self.test_button)
        layout.addWidget(self.disconnect_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._render_status()

    def _render_status(self, status: Any | None = None) -> None:
        status = status or self.service.status()
        account = f"\nAccount: {status.account}" if status.account else ""
        health = ""
        if status.healthy is True:
            health = "\nHealth: Ready"
        elif status.healthy is False:
            health = "\nHealth: Connection failed"
        self.status_label.setText(f"{status.message or ''}{account}{health}".strip())
        self.test_button.setEnabled(status.connected and not self._busy)
        self.disconnect_button.setEnabled(status.connected and not self._busy)
        self.connect_button.setEnabled(not self._busy)
        self.advanced_toggle.setEnabled(not self._busy)
        advanced_enabled = self.advanced_toggle.isChecked() and not self._busy
        self.client_id_input.setEnabled(advanced_enabled)
        self.client_secret_input.setEnabled(advanced_enabled)

    def _advanced_toggled(self, checked: bool) -> None:
        self.advanced_widget.setVisible(checked)
        self._render_status()

    def _connect(self) -> None:
        use_override = self.advanced_toggle.isChecked()
        client_id = self.client_id_input.text().strip() if use_override else None
        if use_override and not client_id:
            QMessageBox.warning(
                self,
                "OAuth Client ID Required",
                "Enter the Desktop OAuth client ID created in Google Cloud Console.",
            )
            return
        self._set_busy("Waiting for Google sign-in in your browser…")
        worker = AsyncWorker(
            self.service.connect(
                client_id,
                client_secret=(
                    self.client_secret_input.text().strip() or None
                    if use_override
                    else None
                ),
            )
        )
        self._start(worker, changed=True)

    def _test(self) -> None:
        self._set_busy("Testing Google Calendar…")
        self._start(AsyncWorker(self.service.test()))

    def _disconnect(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Disconnect Google Calendar",
                "Revoke Google authorization and remove the encrypted local tokens?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._set_busy("Revoking Google authorization…")
        self._start(AsyncWorker(self.service.disconnect()), changed=True)

    def _start(self, worker: AsyncWorker, *, changed: bool = False) -> None:
        worker.complete.connect(lambda status: self._operation_complete(status, changed=changed))
        worker.failed.connect(self._operation_failed)
        worker.finished.connect(lambda: self._worker_finished(worker))
        self._workers.append(worker)
        worker.start()

    def _operation_complete(self, status: Any, *, changed: bool) -> None:
        self._busy = False
        self._render_status(status)
        if changed:
            self.connection_changed.emit()

    def _operation_failed(self, error: str) -> None:
        self._busy = False
        self._render_status()
        self.status_label.setText(f"Connection operation failed: {error}")

    def _worker_finished(self, worker: AsyncWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)

    def _set_busy(self, message: str) -> None:
        self._busy = True
        self.status_label.setText(message)
        self.connect_button.setEnabled(False)
        self.test_button.setEnabled(False)
        self.disconnect_button.setEnabled(False)
        self.advanced_toggle.setEnabled(False)
        self.client_id_input.setEnabled(False)
        self.client_secret_input.setEnabled(False)
