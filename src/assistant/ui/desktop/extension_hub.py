from __future__ import annotations

import importlib
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from assistant.extensions.platform import CatalogExtension, ExtensionHubService
from assistant.ui.desktop.workers import AsyncWorker


class ExtensionHubPage(QWidget):
    """Browse a configured extension platform and manage reviewed packages."""

    installed = Signal(str)
    uninstalled = Signal(str)
    connection_changed = Signal(str)

    def __init__(
        self,
        service: ExtensionHubService,
        *,
        offline: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.offline = offline
        self._entries: tuple[CatalogExtension, ...] = ()
        self._workers: list[AsyncWorker] = []
        self._busy = False
        self._completion_message: str | None = None
        self.setObjectName("assistantPage")
        self._build_ui()
        self.set_configuration(service.catalog_url, offline=offline)
        if service.catalog_url and not offline:
            QTimer.singleShot(0, self.refresh)

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(18)

        browser = QFrame()
        browser.setObjectName("skillBrowserCard")
        browser.setMinimumWidth(270)
        browser.setMaximumWidth(360)
        browser_layout = QVBoxLayout(browser)
        browser_layout.setContentsMargins(16, 16, 16, 16)
        browser_layout.setSpacing(10)

        title = QLabel("EXTENSION HUB")
        title.setObjectName("sectionLabel")
        browser_layout.addWidget(title)
        explanation = QLabel(
            "Browse extensions published by the configured platform. Packages are verified "
            "and installed disabled with no permission grants."
        )
        explanation.setWordWrap(True)
        explanation.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        browser_layout.addWidget(explanation)

        self.platform_label = QLabel("")
        self.platform_label.setObjectName("activityHint")
        self.platform_label.setWordWrap(True)
        self.platform_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        browser_layout.addWidget(self.platform_label)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("extensionSearch")
        self.search_input.setAccessibleName("Search extensions")
        self.search_input.setPlaceholderText("Search extensions")
        self.search_input.textChanged.connect(self._render_entries)
        browser_layout.addWidget(self.search_input)

        self.extension_list = QListWidget()
        self.extension_list.setObjectName("extensionList")
        self.extension_list.setAccessibleName("Platform extensions")
        self.extension_list.setWordWrap(True)
        self.extension_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.extension_list.currentItemChanged.connect(lambda *_: self._show_selected())
        browser_layout.addWidget(self.extension_list, 1)

        self.refresh_button = QPushButton("Refresh Catalog")
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.clicked.connect(self.refresh)
        browser_layout.addWidget(self.refresh_button)

        detail_scroll = QScrollArea()
        detail_scroll.setObjectName("skillDetailScroll")
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail = QWidget()
        detail.setObjectName("skillDetailPage")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(4, 2, 10, 16)
        detail_layout.setSpacing(12)

        self.detail_title = QLabel("Select an extension")
        self.detail_title.setObjectName("skillDetailTitle")
        self.detail_title.setWordWrap(True)
        self.detail_id = QLabel("")
        self.detail_id.setObjectName("skillDetailId")
        self.detail_id.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.detail_id)

        overview = QFrame()
        overview.setObjectName("skillDetailCard")
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(18, 16, 18, 16)
        badges = QHBoxLayout()
        self.category_badge = QLabel("General")
        self.category_badge.setObjectName("skillCategoryBadge")
        self.security_badge = QLabel("Not loaded")
        self.security_badge.setObjectName("skillSecurityBadge")
        badges.addWidget(self.category_badge)
        badges.addWidget(self.security_badge)
        badges.addStretch()
        self.publisher_label = QLabel("")
        self.publisher_label.setObjectName("activityHint")
        self.description_label = QLabel("")
        self.description_label.setObjectName("skillDescription")
        self.description_label.setWordWrap(True)
        overview_layout.addLayout(badges)
        overview_layout.addWidget(self.publisher_label)
        overview_layout.addWidget(self.description_label)
        detail_layout.addWidget(overview)

        permission_heading = QLabel("DECLARED PERMISSIONS")
        permission_heading.setObjectName("sectionLabel")
        detail_layout.addWidget(permission_heading)
        self.permission_table = QTableWidget(0, 3)
        self.permission_table.setObjectName("extensionPermissionTable")
        self.permission_table.setHorizontalHeaderLabels(
            ["Capability", "Applies to", "Why it is needed"]
        )
        self.permission_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.permission_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.permission_table.setAlternatingRowColors(True)
        self.permission_table.setWordWrap(True)
        self.permission_table.setMinimumHeight(120)
        self.permission_table.setMaximumHeight(180)
        for column in range(2):
            self.permission_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.permission_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        detail_layout.addWidget(self.permission_table)

        install_card = QFrame()
        install_card.setObjectName("skillDetailCard")
        install_layout = QVBoxLayout(install_card)
        install_layout.setContentsMargins(18, 16, 18, 16)
        install_heading = QLabel("LOCAL INSTALLATION")
        install_heading.setObjectName("sectionLabel")
        self.install_status = QLabel("Select an extension to review it.")
        self.install_status.setObjectName("activityHint")
        self.install_status.setWordWrap(True)
        self.install_button = QPushButton("Install Extension")
        self.install_button.setObjectName("primaryButton")
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self.install_selected)
        self.uninstall_button = QPushButton("Uninstall Extension")
        self.uninstall_button.setObjectName("dangerButton")
        self.uninstall_button.setVisible(False)
        self.uninstall_button.setEnabled(False)
        self.uninstall_button.clicked.connect(self.uninstall_selected)
        self.connection_button = QPushButton("Configure Account")
        self.connection_button.setObjectName("secondaryButton")
        self.connection_button.setVisible(False)
        self.connection_button.clicked.connect(self.configure_connection)
        install_layout.addWidget(install_heading)
        install_layout.addWidget(self.install_status)
        install_layout.addWidget(self.connection_button, 0, Qt.AlignmentFlag.AlignRight)
        install_layout.addWidget(self.install_button, 0, Qt.AlignmentFlag.AlignRight)
        install_layout.addWidget(self.uninstall_button, 0, Qt.AlignmentFlag.AlignRight)
        detail_layout.addWidget(install_card)
        detail_layout.addStretch()
        detail_scroll.setWidget(detail)

        layout.addWidget(browser, 1)
        layout.addWidget(detail_scroll, 3)

    def set_configuration(self, catalog_url: str | None, *, offline: bool) -> None:
        self.offline = offline
        self._entries = self.service.catalog.extensions
        self._render_entries()
        self.platform_label.setText(
            catalog_url or "First-party extensions included with this build"
        )
        self.refresh_button.setEnabled(bool(catalog_url) and not offline and not self._busy)
        if not catalog_url and not self._entries:
            self.install_status.setText(
                "Configure an HTTPS extension catalog URL in Settings to browse extensions."
            )
        elif not catalog_url:
            self.install_status.setText(
                "First-party extensions can be installed without an external catalog."
            )
        elif offline:
            self.install_status.setText(
                "The global offline lock is active. Disable it in Settings and restart to "
                "connect to the extension platform."
            )

    def focus_extension(self, extension_id: str) -> bool:
        """Select the exact extension requested by another settings surface."""
        self.search_input.clear()
        if not extension_id:
            return False
        for row in range(self.extension_list.count()):
            item = self.extension_list.item(row)
            if str(item.data(Qt.ItemDataRole.UserRole)) == extension_id:
                self.extension_list.setCurrentItem(item)
                self.extension_list.scrollToItem(item)
                return True
        self.search_input.setText(extension_id.replace("integration.", "").replace("_", " "))
        return False

    def refresh(self) -> None:
        if self._busy or self.offline or not self.service.catalog_url:
            return
        self._set_busy(True, "Loading extension catalog…")
        worker = AsyncWorker(self.service.refresh())
        worker.complete.connect(self._catalog_loaded)
        worker.failed.connect(self._operation_failed)
        worker.finished.connect(lambda: self._worker_finished(worker))
        self._workers.append(worker)
        worker.start()

    def _catalog_loaded(self, entries: Any) -> None:
        self._busy = False
        self._entries = tuple(entries)
        self._render_entries()
        count = len(self._entries)
        self.install_status.setText(
            f"Loaded {count} extension{'s' if count != 1 else ''} from the platform."
        )

    def _render_entries(self, *_args: Any) -> None:
        selected = self._selected_id()
        query = self.search_input.text().strip().casefold()
        self.extension_list.clear()
        for extension in self._entries:
            haystack = " ".join(
                (
                    extension.display_name,
                    extension.description,
                    extension.publisher,
                    extension.category,
                )
            ).casefold()
            if query and query not in haystack:
                continue
            status = self.service.installation_status(extension.extension_id)
            state = {
                "available": "Available",
                "installed": "Installed",
                "id_in_use": "ID already in use",
            }[status]
            if not extension.compatible:
                state = "Unsupported by this build"
            item = QListWidgetItem(
                f"{extension.display_name}\n{extension.publisher}  ·  v{extension.version}  ·  {state}"
            )
            item.setData(Qt.ItemDataRole.UserRole, extension.extension_id)
            item.setToolTip(extension.description)
            self.extension_list.addItem(item)
            if extension.extension_id == selected:
                self.extension_list.setCurrentItem(item)
        if self.extension_list.currentRow() < 0 and self.extension_list.count():
            self.extension_list.setCurrentRow(0)
        if not self.extension_list.count():
            self._show_selected()

    def _selected_id(self) -> str | None:
        item = self.extension_list.currentItem()
        if item is None:
            return None
        return str(item.data(Qt.ItemDataRole.UserRole))

    def _selected(self) -> CatalogExtension | None:
        selected_id = self._selected_id()
        return next(
            (item for item in self._entries if item.extension_id == selected_id),
            None,
        )

    def _show_selected(self) -> None:
        extension = self._selected()
        if extension is None:
            self.detail_title.setText("No extension selected")
            self.detail_id.clear()
            self.publisher_label.clear()
            self.description_label.clear()
            self.permission_table.setRowCount(0)
            self.install_button.setText("Install Extension")
            self.install_button.setEnabled(False)
            self.uninstall_button.setVisible(False)
            self.uninstall_button.setEnabled(False)
            self.connection_button.setVisible(False)
            return

        self.detail_title.setText(extension.display_name)
        self.detail_id.setText(
            f"{extension.extension_id}  ·  version {extension.version}  ·  "
            f"API {extension.api_version}"
        )
        self.category_badge.setText(extension.category)
        self.security_badge.setText(f"{extension.risk.value.title()} risk")
        self.security_badge.setProperty("level", extension.risk.value)
        self.security_badge.style().unpolish(self.security_badge)
        self.security_badge.style().polish(self.security_badge)
        self.publisher_label.setText(
            f"Published by {extension.publisher}  ·  Managed first-party component"
            if extension.source == "first_party"
            else (
                f"Published by {extension.publisher}  ·  {extension.extension_type.title()} package"
            )
        )
        self.description_label.setText(extension.description)
        permissions = extension.permissions
        self.permission_table.setRowCount(max(1, len(permissions)))
        if not permissions:
            values = ("None", "Local extension boundary", "No protected capability declared.")
            for column, value in enumerate(values):
                self.permission_table.setItem(0, column, QTableWidgetItem(value))
        else:
            for row, permission in enumerate(permissions):
                targets = "; ".join(permission.targets) if permission.targets else "Any target"
                for column, value in enumerate((permission.capability, targets, permission.reason)):
                    self.permission_table.setItem(row, column, QTableWidgetItem(value))
        self.permission_table.resizeRowsToContents()

        status = self.service.installation_status(extension.extension_id)
        connection_service = self._connection_service(extension.extension_id)
        dialog_entry_point = self._connection_dialog_entry_point(extension.extension_id)
        self.connection_button.setVisible(
            connection_service is not None
            and dialog_entry_point is not None
            and status == "installed"
        )
        self.connection_button.setEnabled(not self._busy and not self.offline)
        self.uninstall_button.setVisible(status == "installed")
        self.uninstall_button.setEnabled(status == "installed" and not self._busy)
        self.connection_button.setToolTip(
            "Disable the global offline lock before connecting an account." if self.offline else ""
        )
        if connection_service is not None and status == "installed":
            connection_status = self._connection_status(extension.extension_id)
            connected = bool(
                getattr(
                    connection_status,
                    "authenticated",
                    getattr(connection_status, "connected", False),
                )
            )
            if extension.extension_id == "integration.openai":
                self.connection_button.setText("Configure OpenAI / Codex")
            else:
                self.connection_button.setText(
                    "Manage Connected Account" if connected else "Connect Account"
                )
        installable = (
            self.service.is_installable(extension)
            if hasattr(self.service, "is_installable")
            else extension.compatible
        )
        if not installable or status in {"id_in_use", "installed"}:
            self.install_button.setText("Installed" if status == "installed" else "Unavailable")
        else:
            self.install_button.setText("Install Extension")
        enabled = installable and status == "available" and not self._busy
        self.install_button.setEnabled(enabled)
        if not extension.compatible:
            message = (
                f"This build supports API 1.0 skill packages, not "
                f"{extension.extension_type} API {extension.api_version}."
            )
        elif not installable:
            message = (
                "Downloaded Python extensions are blocked in this build because a process and "
                "Job Object are not an OS-enforced hostile-code sandbox. Use reviewed first-party "
                "extensions instead."
            )
        elif (
            status == "installed"
            and self._connection_state(extension.extension_id) == "missing_scopes"
        ):
            message = (
                "The account is connected but this extension needs additional access. "
                "Manage the connected account to review the missing scopes."
            )
        elif status == "installed" and self._connection_state(extension.extension_id) in {
            "reauthorization_required",
            "error",
        }:
            message = (
                "The connected account needs attention before this extension can run. "
                "Manage the account to reconnect or review access."
            )
        elif status == "installed":
            message = "Installed locally. Review and enable it from the Skills tab."
        elif status == "id_in_use":
            message = "This extension ID is already used by a built-in skill."
        elif extension.source == "first_party":
            message = (
                "Ready to install. This first-party implementation ships with the "
                "application and remains disabled until you review and enable it."
            )
        else:
            message = (
                "Ready to install. The package digest, trusted signature, manifest, and "
                "catalog metadata will be verified first."
            )
        self.install_status.setText(message)

    def install_selected(self) -> None:
        extension = self._selected()
        if extension is None or self._busy:
            return
        permission_names = (
            ", ".join(permission.capability for permission in extension.permissions) or "none"
        )
        answer = QMessageBox.question(
            self,
            "Install Extension",
            f"Install {extension.display_name} from {extension.publisher}?\n\n"
            f"Declared permissions: {permission_names}\n\n"
            "The extension will be installed disabled and will receive no permission grants.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._completion_message = None
        action = (
            f"Installing {extension.display_name} from this application build…"
            if extension.source == "first_party"
            else f"Downloading and verifying {extension.display_name}…"
        )
        self._set_busy(True, action)
        worker = AsyncWorker(self.service.install(extension.extension_id))
        worker.complete.connect(self._extension_installed)
        worker.failed.connect(self._operation_failed)
        worker.finished.connect(lambda: self._worker_finished(worker))
        self._workers.append(worker)
        worker.start()

    def uninstall_selected(self) -> None:
        extension = self._selected()
        if extension is None or self._busy:
            return
        if self.service.installation_status(extension.extension_id) != "installed":
            return
        uninstall = getattr(self.service, "uninstall", None)
        if not callable(uninstall):
            self._operation_failed("This extension hub does not support uninstalling extensions.")
            return
        answer = QMessageBox.question(
            self,
            "Uninstall Extension",
            f"Uninstall {extension.display_name}?\n\n"
            "This removes the local extension package and disables it. "
            "You can install it again from the hub later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._completion_message = None
        self._set_busy(True, f"Uninstalling {extension.display_name}…")
        extension_id = extension.extension_id
        worker = AsyncWorker(uninstall(extension_id))
        worker.complete.connect(lambda _result: self._extension_uninstalled(extension_id))
        worker.failed.connect(self._operation_failed)
        worker.finished.connect(lambda: self._worker_finished(worker))
        self._workers.append(worker)
        worker.start()

    def configure_connection(self) -> None:
        extension = self._selected()
        if extension is None:
            return
        service = self._connection_service(extension.extension_id)
        if service is None:
            return
        entry_point = self._connection_dialog_entry_point(extension.extension_id)
        if entry_point is None:
            return
        module_name, _, object_name = entry_point.partition(":")
        try:
            dialog_factory = getattr(importlib.import_module(module_name), object_name)
            dialog_service_resolver = getattr(self.service, "connection_dialog_service", None)
            dialog_service = (
                dialog_service_resolver(extension.extension_id)
                if callable(dialog_service_resolver)
                else service
            )
            dialog = dialog_factory(dialog_service, self)
        except Exception as error:
            self._operation_failed(f"The account configuration dialog could not be loaded: {error}")
            return
        if not hasattr(dialog, "connection_changed") or not hasattr(dialog, "exec"):
            self._operation_failed("The account configuration dialog is invalid.")
            return
        dialog.connection_changed.connect(
            lambda: self._sync_connection_state(extension.extension_id, notify=False)
        )
        dialog.exec()
        self._sync_connection_state(extension.extension_id)

    def _sync_connection_state(self, extension_id: str, *, notify: bool = True) -> None:
        sync = getattr(self.service, "sync_connection_state", None)
        if callable(sync):
            sync(extension_id)
        self._show_selected()
        if notify:
            self.connection_changed.emit(extension_id)

    def _connection_service(self, extension_id: str) -> Any | None:
        resolver = getattr(self.service, "connection_service", None)
        return resolver(extension_id) if resolver else None

    def _connection_dialog_entry_point(self, extension_id: str) -> str | None:
        resolver = getattr(self.service, "connection_dialog_entry_point", None)
        return resolver(extension_id) if resolver else None

    def _connection_status(self, extension_id: str) -> Any | None:
        resolver = getattr(self.service, "connection_status", None)
        if callable(resolver):
            return resolver(extension_id)
        connection = self._connection_service(extension_id)
        return connection.status() if connection is not None else None

    def _connection_state(self, extension_id: str) -> str | None:
        status = self._connection_status(extension_id)
        state = getattr(status, "state", None)
        return getattr(state, "value", str(state)) if state is not None else None

    def _extension_installed(self, manifest: Any) -> None:
        self.install_status.setText(
            f"{manifest.display_name} was installed disabled. Review it in the Skills tab "
            "before enabling it."
        )
        self.installed.emit(manifest.skill_id)
        self._render_entries()

    def _extension_uninstalled(self, extension_id: str) -> None:
        extension = next(
            (item for item in self._entries if item.extension_id == extension_id),
            None,
        )
        name = extension.display_name if extension is not None else extension_id
        self._completion_message = f"{name} was uninstalled from this computer."
        self.uninstalled.emit(extension_id)
        self._render_entries()
        self.install_status.setText(self._completion_message)

    def _operation_failed(self, error: str) -> None:
        self.install_status.setText(f"Extension operation failed: {error}")

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        self.refresh_button.setEnabled(
            bool(self.service.catalog_url) and not self.offline and not busy
        )
        self.install_button.setEnabled(False)
        self.uninstall_button.setEnabled(False)
        if message:
            self.install_status.setText(message)

    def _worker_finished(self, worker: AsyncWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        self._set_busy(False)
        self._show_selected()
        if self._completion_message:
            self.install_status.setText(self._completion_message)
            self._completion_message = None
