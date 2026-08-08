from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
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
class ApiTokenConnectionDialogConfig:
    provider_name: str
    window_title: str
    description: str
    object_prefix: str
    token_label: str
    token_placeholder: str
    connect_label: str
    test_busy_message: str
    disconnect_title: str
    disconnect_question: str
    disconnect_busy_message: str


class ApiTokenConnectionDialog(QDialog):
    connection_changed = Signal()

    def __init__(
        self,
        service: Any,
        config: ApiTokenConnectionDialogConfig,
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

        form = QFormLayout()
        self.token_input = QLineEdit()
        self.token_input.setObjectName(f"{config.object_prefix}Token")
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText(config.token_placeholder)
        form.addRow(config.token_label, self.token_input)
        layout.addLayout(form)

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
        self.status_label.setText(f"{status.message or ''}{account}{health}".strip())
        usable = bool(getattr(status, "usable", status.connected))
        authenticated = bool(getattr(status, "authenticated", status.connected))
        self.test_button.setEnabled(usable and not self._busy)
        self.disconnect_button.setEnabled(authenticated and not self._busy)
        self.connect_button.setEnabled(not self._busy)
        self.token_input.setEnabled(not self._busy)

    def _connect(self) -> None:
        token = self.token_input.text().strip()
        if not token:
            QMessageBox.warning(
                self, "Token Required", f"Enter a {self.config.provider_name} token."
            )
            return
        self._set_busy(f"Connecting {self.config.provider_name}…")
        self._start(AsyncWorker(self.service.connect(token)), changed=True)

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
        self.token_input.setEnabled(False)
