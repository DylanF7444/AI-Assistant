from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from assistant.dashboard.contracts import ProviderConnection, ProviderHealth
from assistant.ui.desktop.controller import DesktopController


class HomeAssistantConnectionDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Home Assistant")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        description = QLabel(
            "Connect one Home Assistant instance. The token is encrypted for the "
            "current Windows account and is never stored in dashboard settings."
        )
        description.setWordWrap(True)
        description.setObjectName("pageSubtitle")
        layout.addWidget(description)
        form = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setText("Home Assistant")
        self.name_input.setObjectName("homeAssistantNameInput")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://home.example.com")
        self.url_input.setObjectName("homeAssistantUrlInput")
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setObjectName("homeAssistantTokenInput")
        self.history_input = QCheckBox("Keep opt-in numeric trend history")
        self.history_input.setObjectName("homeAssistantHistoryInput")
        form.addRow("Display name", self.name_input)
        form.addRow("Base URL", self.url_input)
        form.addRow("Long-lived token", self.token_input)
        form.addRow("", self.history_input)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def payload(self) -> dict:
        return {
            "display_name": " ".join(self.name_input.text().split()),
            "endpoint": self.url_input.text().strip(),
            "token": self.token_input.text().strip(),
            "history_enabled": self.history_input.isChecked(),
        }

    def _accept(self) -> None:
        payload = self.payload()
        if not payload["display_name"] or not payload["endpoint"] or not payload["token"]:
            QMessageBox.warning(
                self,
                "Connection Is Incomplete",
                "Display name, base URL, and token are required.",
            )
            return
        self.accept()


class DashboardConnectionsDialog(QDialog):
    add_requested = Signal(object)
    test_requested = Signal(object)
    enabled_requested = Signal(object)
    history_requested = Signal(object)
    clear_history_requested = Signal(object)
    remove_requested = Signal(object)

    def __init__(
        self,
        controller: DesktopController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("dashboardConnectionsDialog")
        self.setWindowTitle("Dashboard Connections")
        self.setMinimumSize(680, 460)
        self._busy = False

        root = QVBoxLayout(self)
        title = QLabel("Dashboard connections")
        title.setObjectName("skillDetailTitle")
        description = QLabel(
            "External providers are read-only, permission-gated, and paused by "
            "the global offline lock."
        )
        description.setObjectName("pageSubtitle")
        description.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(description)

        content = QHBoxLayout()
        self.connection_list = QListWidget()
        self.connection_list.setObjectName("dashboardConnectionList")
        self.connection_list.currentItemChanged.connect(self._selection_changed)
        content.addWidget(self.connection_list, 1)
        detail = QVBoxLayout()
        self.connection_name = QLabel("Select a connection.")
        self.connection_name.setObjectName("dashboardCardTitle")
        self.connection_detail = QLabel("")
        self.connection_detail.setObjectName("pageSubtitle")
        self.connection_detail.setWordWrap(True)
        self.operation_status = QLabel("")
        self.operation_status.setObjectName("activityHint")
        self.operation_status.setWordWrap(True)
        detail.addWidget(self.connection_name)
        detail.addWidget(self.connection_detail)
        detail.addWidget(self.operation_status)
        detail.addStretch()
        content.addLayout(detail, 2)
        root.addLayout(content, 1)

        controls = QHBoxLayout()
        self.add_button = QPushButton("Add Home Assistant")
        self.add_button.setObjectName("primaryButton")
        self.add_button.clicked.connect(self._add)
        self.test_button = QPushButton("Test")
        self.test_button.clicked.connect(self._test)
        self.toggle_button = QPushButton("Disable")
        self.toggle_button.clicked.connect(self._toggle)
        self.history_button = QPushButton("Enable history")
        self.history_button.clicked.connect(self._toggle_history)
        self.clear_history_button = QPushButton("Clear history")
        self.clear_history_button.clicked.connect(self._clear_history)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("dangerButton")
        self.remove_button.clicked.connect(self._remove)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        controls.addWidget(self.add_button)
        controls.addWidget(self.test_button)
        controls.addWidget(self.toggle_button)
        controls.addWidget(self.history_button)
        controls.addWidget(self.clear_history_button)
        controls.addWidget(self.remove_button)
        controls.addStretch()
        controls.addWidget(close_button)
        root.addLayout(controls)
        self.reload_connections()

    def reload_connections(self, selected_id: UUID | None = None) -> None:
        self.connection_list.clear()
        connections = self.controller.dashboard_connections()
        for connection in connections:
            label = connection.display_name
            if not connection.enabled:
                label += " (disabled)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(connection.connection_id))
            self.connection_list.addItem(item)
        if not connections:
            self._selection_changed(None, None)
            return
        row = 0
        if selected_id is not None:
            row = next(
                (
                    index
                    for index, connection in enumerate(connections)
                    if connection.connection_id == selected_id
                ),
                0,
            )
        self.connection_list.setCurrentRow(row)

    def connection_added(self, connection: ProviderConnection) -> None:
        self._set_busy(False)
        self.operation_status.setText("Connection saved and health check passed.")
        self.reload_connections(connection.connection_id)

    def connection_changed(self, connection: ProviderConnection | None = None) -> None:
        self._set_busy(False)
        self.operation_status.setText("Connection settings updated.")
        self.reload_connections(connection.connection_id if connection else None)

    def health_received(self, health: ProviderHealth) -> None:
        self._set_busy(False)
        self.operation_status.setText(
            health.message or ("Connection is healthy." if health.healthy else "Connection failed.")
        )

    def operation_failed(self, message: str) -> None:
        self._set_busy(False)
        self.operation_status.setText(f"Operation failed: {message}")

    def _add(self) -> None:
        dialog = HomeAssistantConnectionDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._set_busy(True)
        self.operation_status.setText("Saving and testing connection…")
        self.add_requested.emit(dialog.payload())

    def _test(self) -> None:
        connection = self._selected_connection()
        if connection is None:
            return
        self._set_busy(True)
        self.operation_status.setText("Testing connection…")
        self.test_requested.emit(connection.connection_id)

    def _toggle(self) -> None:
        connection = self._selected_connection()
        if connection is None:
            return
        self.enabled_requested.emit((connection.connection_id, not connection.enabled))

    def _remove(self) -> None:
        connection = self._selected_connection()
        if connection is None:
            return
        if (
            QMessageBox.question(
                self,
                "Remove Dashboard Connection",
                f"Remove {connection.display_name}, its cards, and its encrypted token?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.remove_requested.emit(connection.connection_id)

    def _toggle_history(self) -> None:
        connection = self._selected_connection()
        if connection is None:
            return
        self.history_requested.emit((connection.connection_id, not connection.history_enabled))

    def _clear_history(self) -> None:
        connection = self._selected_connection()
        if connection is None:
            return
        if (
            QMessageBox.question(
                self,
                "Clear Dashboard History",
                f"Delete all retained numeric history for {connection.display_name}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.clear_history_requested.emit(connection.connection_id)

    def _selection_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        connection = self._selected_connection(current)
        enabled = connection is not None and not self._busy
        self.test_button.setEnabled(enabled)
        self.toggle_button.setEnabled(enabled)
        self.history_button.setEnabled(enabled)
        self.clear_history_button.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)
        if connection is None:
            self.connection_name.setText("No connections configured")
            self.connection_detail.setText(
                "Add Home Assistant to make its entity metrics available."
            )
            return
        self.connection_name.setText(connection.display_name)
        self.connection_detail.setText(
            f"Provider: Home Assistant\n"
            f"Endpoint: {connection.endpoint}\n"
            f"Status: {'Enabled' if connection.enabled else 'Disabled'}\n"
            f"Numeric history: {'On' if connection.history_enabled else 'Off'}"
        )
        self.toggle_button.setText("Disable" if connection.enabled else "Enable")
        self.history_button.setText(
            "Disable history" if connection.history_enabled else "Enable history"
        )

    def _selected_connection(
        self,
        item: QListWidgetItem | None = None,
    ) -> ProviderConnection | None:
        selected = item or self.connection_list.currentItem()
        if selected is None:
            return None
        connection_id = UUID(str(selected.data(Qt.ItemDataRole.UserRole)))
        return next(
            (
                connection
                for connection in self.controller.dashboard_connections()
                if connection.connection_id == connection_id
            ),
            None,
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.add_button.setEnabled(not busy)
        self.connection_list.setEnabled(not busy)
        self._selection_changed(self.connection_list.currentItem(), None)
