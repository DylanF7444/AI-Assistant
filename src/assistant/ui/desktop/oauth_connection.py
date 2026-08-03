from __future__ import annotations

from dataclasses import dataclass
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

from assistant.ui.desktop.workers import AsyncWorker


@dataclass(frozen=True)
class OAuthConnectionDialogConfig:
    provider_name: str
    window_title: str
    description: str
    object_prefix: str
    test_busy_message: str
    disconnect_title: str
    disconnect_question: str
    disconnect_busy_message: str
    client_id_placeholder: str = "Desktop OAuth client ID"
    connect_label: str = "Sign in"


class OAuthConnectionDialog(QDialog):
    """Reusable account UI for reviewed desktop OAuth connection services."""

    connection_changed = Signal()

    def __init__(
        self,
        service: Any,
        config: OAuthConnectionDialogConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.config = config
        self._workers: list[AsyncWorker] = []
        self._busy = False
        self.setWindowTitle(config.window_title)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)

        description = QLabel(config.description)
        description.setWordWrap(True)
        description.setObjectName("pageSubtitle")
        layout.addWidget(description)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("activityHint")
        layout.addWidget(self.status_label)

        self.advanced_toggle = QCheckBox("Configure OAuth client credentials")
        self.advanced_toggle.setObjectName(f"{config.object_prefix}AdvancedToggle")
        layout.addWidget(self.advanced_toggle)

        oauth_hint = QLabel(
            "If the provider reports that client_secret is missing, expand this "
            "section and enter the OAuth application secret. It is reused automatically "
            "for future sign-ins and token refreshes."
        )
        oauth_hint.setWordWrap(True)
        oauth_hint.setObjectName("activityHint")
        layout.addWidget(oauth_hint)

        self.advanced_widget = QWidget()
        self.advanced_widget.setObjectName(f"{config.object_prefix}AdvancedSettings")
        form = QFormLayout(self.advanced_widget)
        self.client_id_input = QLineEdit()
        self.client_id_input.setObjectName(f"{config.object_prefix}ClientId")
        self.client_id_input.setPlaceholderText(config.client_id_placeholder)
        configured_client_id = getattr(service, "configured_client_id", None)
        if callable(configured_client_id):
            configured_client_id = configured_client_id()
        self.client_id_input.setText(
            str(configured_client_id or getattr(service, "default_client_id", ""))
        )
        self.client_secret_input = QLineEdit()
        self.client_secret_input.setObjectName(f"{config.object_prefix}ClientSecret")
        self.client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret_input.setPlaceholderText(
            "Enter once; saved encrypted for future sign-ins"
        )
        form.addRow("OAuth client ID", self.client_id_input)
        form.addRow("OAuth client secret", self.client_secret_input)
        self.advanced_widget.setVisible(False)
        self.advanced_toggle.toggled.connect(self._advanced_toggled)
        layout.addWidget(self.advanced_widget)

        self.connect_button = QPushButton(config.connect_label)
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
        missing_scopes = tuple(getattr(status, "missing_scopes", ()) or ())
        access = ""
        if missing_scopes:
            access = f"\nAccess needed: {', '.join(missing_scopes[:5])}"
        self.status_label.setText(f"{status.message or ''}{account}{health}{access}".strip())
        usable = bool(getattr(status, "usable", status.connected))
        authenticated = bool(getattr(status, "authenticated", status.connected))
        self.test_button.setEnabled(usable and not self._busy)
        self.disconnect_button.setEnabled(authenticated and not self._busy)
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
                "Enter the Desktop OAuth client ID created for this application.",
            )
            return
        self._set_busy(f"Waiting for {self.config.provider_name} sign-in in your browser…")
        worker = AsyncWorker(
            self.service.connect(
                client_id,
                client_secret=(
                    self.client_secret_input.text().strip() or None if use_override else None
                ),
            )
        )
        self._start(worker, changed=True)

    def _test(self) -> None:
        self._set_busy(self.config.test_busy_message)
        self._start(AsyncWorker(self.service.test()))

    def _disconnect(self) -> None:
        if (
            QMessageBox.question(
                self,
                self.config.disconnect_title,
                self.config.disconnect_question,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._set_busy(self.config.disconnect_busy_message)
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
        lowered = error.casefold()
        if "client_secret" in lowered or ("client" in lowered and "secret" in lowered):
            self.advanced_toggle.setChecked(True)
            self.status_label.setText(
                "The provider requires this OAuth client secret. Enter it below once "
                "and retry; Local Assistant stores it encrypted for future connections."
            )
        else:
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
