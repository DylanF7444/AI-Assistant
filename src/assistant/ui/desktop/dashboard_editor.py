from __future__ import annotations

from uuid import UUID, uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from assistant.dashboard.contracts import (
    DashboardCard,
    DashboardLayout,
    MetricPresentation,
)
from assistant.ui.desktop.controller import DesktopController

_REFRESH_INTERVALS = (15, 30, 60, 300, 900, 1800, 3600)


class DashboardEditor(QDialog):
    def __init__(
        self,
        controller: DesktopController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("dashboardEditor")
        self.setWindowTitle("Customize Dashboard")
        self.setMinimumSize(760, 520)
        self._catalog = {
            definition.metric_id: definition for definition in controller.dashboard_catalog()
        }
        self._connections = controller.dashboard_connections()
        self._cards = list(
            sorted(controller.dashboard_layout().cards, key=lambda item: item.position)
        )
        self._loading_fields = False
        self._result_layout: DashboardLayout | None = None

        root = QVBoxLayout(self)
        title = QLabel("Customize dashboard")
        title.setObjectName("skillDetailTitle")
        subtitle = QLabel(
            "Choose metrics, ordering, size, presentation, and refresh rate. "
            "Changes are saved only when you select Save."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        add_row = QHBoxLayout()
        self.metric_catalog = QComboBox()
        self.metric_catalog.setObjectName("dashboardMetricCatalog")
        self.metric_catalog.setEditable(True)
        self.metric_catalog.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for definition in sorted(
            self._catalog.values(),
            key=lambda item: (
                item.requires_connection,
                item.source_label.casefold(),
                item.display_name.casefold(),
            ),
        ):
            self.metric_catalog.addItem(
                f"{definition.display_name} — {definition.source_label}",
                definition.metric_id,
            )
        self.metric_catalog.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.metric_catalog.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.add_button = QPushButton("Add card")
        self.add_button.setObjectName("primaryButton")
        self.add_button.clicked.connect(self._add_card)
        add_row.addWidget(self.metric_catalog, 1)
        add_row.addWidget(self.add_button)
        root.addLayout(add_row)

        content = QHBoxLayout()
        self.card_list = QListWidget()
        self.card_list.setObjectName("dashboardEditorCardList")
        self.card_list.setMinimumWidth(280)
        self.card_list.currentItemChanged.connect(self._selected_card_changed)
        content.addWidget(self.card_list, 1)

        detail = QVBoxLayout()
        self.metric_description = QLabel("Select a card to configure it.")
        self.metric_description.setObjectName("pageSubtitle")
        self.metric_description.setWordWrap(True)
        detail.addWidget(self.metric_description)

        form = QFormLayout()
        self.title_input = QLineEdit()
        self.title_input.setObjectName("dashboardCardTitleInput")
        self.title_input.setPlaceholderText("Use the metric name")
        self.title_input.editingFinished.connect(self._update_selected)
        self.presentation_input = QComboBox()
        self.presentation_input.setObjectName("dashboardPresentationInput")
        self.presentation_input.currentIndexChanged.connect(self._update_selected)
        self.size_input = QComboBox()
        self.size_input.setObjectName("dashboardSizeInput")
        self.size_input.addItem("Small", "small")
        self.size_input.addItem("Medium", "medium")
        self.size_input.addItem("Wide", "wide")
        self.size_input.currentIndexChanged.connect(self._update_selected)
        self.refresh_input = QComboBox()
        self.refresh_input.setObjectName("dashboardRefreshInput")
        for seconds in _REFRESH_INTERVALS:
            self.refresh_input.addItem(_refresh_label(seconds), seconds)
        self.refresh_input.currentIndexChanged.connect(self._update_selected)
        self.enabled_input = QCheckBox("Show this card")
        self.enabled_input.setObjectName("dashboardCardEnabledInput")
        self.enabled_input.toggled.connect(self._update_selected)
        self.connection_input = QComboBox()
        self.connection_input.setObjectName("dashboardConnectionInput")
        self.connection_input.currentIndexChanged.connect(self._update_selected)
        self.entity_input = QLineEdit()
        self.entity_input.setObjectName("dashboardEntityInput")
        self.entity_input.setPlaceholderText("sensor.temperature")
        self.entity_input.editingFinished.connect(self._update_selected)
        self.attribute_input = QLineEdit()
        self.attribute_input.setObjectName("dashboardAttributeInput")
        self.attribute_input.setPlaceholderText("Optional scalar attribute")
        self.attribute_input.editingFinished.connect(self._update_selected)
        self.title_label = QLabel("Title")
        self.presentation_label = QLabel("Presentation")
        self.size_label = QLabel("Size")
        self.refresh_label = QLabel("Refresh")
        self.connection_label = QLabel("Connection")
        self.entity_label = QLabel("Entity ID")
        self.attribute_label = QLabel("Attribute")
        form.addRow(self.title_label, self.title_input)
        form.addRow(self.presentation_label, self.presentation_input)
        form.addRow(self.size_label, self.size_input)
        form.addRow(self.refresh_label, self.refresh_input)
        form.addRow("Visible", self.enabled_input)
        form.addRow(self.connection_label, self.connection_input)
        form.addRow(self.entity_label, self.entity_input)
        form.addRow(self.attribute_label, self.attribute_input)
        detail.addLayout(form)

        move_row = QHBoxLayout()
        self.move_up_button = QPushButton("Move up")
        self.move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self.move_down_button = QPushButton("Move down")
        self.move_down_button.clicked.connect(lambda: self._move_selected(1))
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("dangerButton")
        self.remove_button.clicked.connect(self._remove_selected)
        move_row.addWidget(self.move_up_button)
        move_row.addWidget(self.move_down_button)
        move_row.addStretch()
        move_row.addWidget(self.remove_button)
        detail.addLayout(move_row)
        detail.addStretch()
        content.addLayout(detail, 2)
        root.addLayout(content, 1)

        footer = QHBoxLayout()
        self.reset_button = QPushButton("Reset to defaults")
        self.reset_button.clicked.connect(self._reset_defaults)
        self.dialog_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.dialog_buttons.accepted.connect(self._accept)
        self.dialog_buttons.rejected.connect(self.reject)
        footer.addWidget(self.reset_button)
        footer.addStretch()
        footer.addWidget(self.dialog_buttons)
        root.addLayout(footer)

        self._reload_list()

    def selected_layout(self) -> DashboardLayout:
        if self._result_layout is None:
            raise RuntimeError("Dashboard editor has not been accepted")
        return self._result_layout

    def _add_card(self) -> None:
        metric_id = str(self.metric_catalog.currentData() or "")
        definition = self._catalog.get(metric_id)
        if definition is None:
            return
        matching_connections = tuple(
            connection
            for connection in self._connections
            if connection.provider_id == definition.provider_id and connection.enabled
        )
        if definition.requires_connection and not matching_connections:
            QMessageBox.information(
                self,
                "Connection Required",
                f"Add and enable a {definition.source_label} connection first.",
            )
            return
        card = DashboardCard(
            card_id=uuid4(),
            metric_id=definition.metric_id,
            presentation=definition.default_presentation,
            refresh_seconds=definition.default_refresh_seconds,
            position=len(self._cards),
            connection_id=(
                matching_connections[0].connection_id if definition.requires_connection else None
            ),
            options=(
                {"entity_id": "", "attribute": ""}
                if definition.metric_id == "home_assistant.entity_state"
                else {}
            ),
        )
        self._cards.append(card)
        self._reload_list(card.card_id)

    def _remove_selected(self) -> None:
        row = self.card_list.currentRow()
        if not 0 <= row < len(self._cards):
            return
        self._cards.pop(row)
        self._normalize_positions()
        self._reload_list(
            self._cards[min(row, len(self._cards) - 1)].card_id if self._cards else None
        )

    def _move_selected(self, direction: int) -> None:
        row = self.card_list.currentRow()
        target = row + direction
        if not 0 <= row < len(self._cards) or not 0 <= target < len(self._cards):
            return
        self._update_selected()
        self._cards[row], self._cards[target] = self._cards[target], self._cards[row]
        self._normalize_positions()
        self._reload_list(self._cards[target].card_id)

    def _reset_defaults(self) -> None:
        self._cards = list(
            sorted(
                self.controller.dashboard_default_layout().cards,
                key=lambda item: item.position,
            )
        )
        self._reload_list(self._cards[0].card_id if self._cards else None)

    def _selected_card_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        enabled = current is not None
        for widget in (
            self.title_input,
            self.presentation_input,
            self.size_input,
            self.refresh_input,
            self.enabled_input,
            self.move_up_button,
            self.move_down_button,
            self.remove_button,
            self.connection_input,
            self.entity_input,
            self.attribute_input,
        ):
            widget.setEnabled(enabled)
        if current is None:
            self.metric_description.setText("Select a card to configure it.")
            return
        card = self._card_by_id(UUID(str(current.data(Qt.ItemDataRole.UserRole))))
        definition = self._catalog[card.metric_id]
        self._loading_fields = True
        try:
            self.metric_description.setText(definition.description)
            self.title_input.setText(card.title or "")
            self.presentation_input.clear()
            for presentation in definition.supported_presentations:
                self.presentation_input.addItem(
                    presentation.value.replace("_", " ").title(),
                    presentation.value,
                )
            self.presentation_input.setCurrentIndex(
                max(0, self.presentation_input.findData(card.presentation.value))
            )
            self.size_input.setCurrentIndex(max(0, self.size_input.findData(card.size)))
            refresh_index = self.refresh_input.findData(card.refresh_seconds)
            if refresh_index < 0:
                self.refresh_input.addItem(
                    _refresh_label(card.refresh_seconds),
                    card.refresh_seconds,
                )
                refresh_index = self.refresh_input.count() - 1
            self.refresh_input.setCurrentIndex(refresh_index)
            self.enabled_input.setChecked(card.enabled)
            self.connection_input.clear()
            for connection in self._connections:
                if connection.provider_id == definition.provider_id:
                    label = connection.display_name
                    if not connection.enabled:
                        label += " (disabled)"
                    self.connection_input.addItem(label, str(connection.connection_id))
            if card.connection_id is not None:
                self.connection_input.setCurrentIndex(
                    max(
                        0,
                        self.connection_input.findData(str(card.connection_id)),
                    )
                )
            self.entity_input.setText(str(card.options.get("entity_id", "")))
            self.attribute_input.setText(str(card.options.get("attribute", "")))
        finally:
            self._loading_fields = False
        external = definition.requires_connection
        for widget in (self.connection_label, self.connection_input):
            widget.setVisible(external)
        home_assistant = definition.metric_id == "home_assistant.entity_state"
        for widget in (
            self.entity_label,
            self.entity_input,
            self.attribute_label,
            self.attribute_input,
        ):
            widget.setVisible(home_assistant)
        row = self.card_list.currentRow()
        self.move_up_button.setEnabled(row > 0)
        self.move_down_button.setEnabled(row < len(self._cards) - 1)

    def _update_selected(self) -> None:
        if self._loading_fields:
            return
        row = self.card_list.currentRow()
        if not 0 <= row < len(self._cards):
            return
        presentation = MetricPresentation(
            str(self.presentation_input.currentData() or self._cards[row].presentation.value)
        )
        title = " ".join(self.title_input.text().split()) or None
        definition = self._catalog[self._cards[row].metric_id]
        connection_id = self._cards[row].connection_id
        options = dict(self._cards[row].options)
        if definition.requires_connection:
            connection_data = self.connection_input.currentData()
            connection_id = UUID(str(connection_data)) if connection_data else None
        if definition.metric_id == "home_assistant.entity_state":
            options = {
                "entity_id": self.entity_input.text().strip(),
                "attribute": self.attribute_input.text().strip(),
            }
        self._cards[row] = self._cards[row].model_copy(
            update={
                "title": title,
                "presentation": presentation,
                "size": str(self.size_input.currentData() or "medium"),
                "refresh_seconds": int(self.refresh_input.currentData() or 60),
                "enabled": self.enabled_input.isChecked(),
                "connection_id": connection_id,
                "options": options,
            }
        )
        self._update_list_item(row)

    def _reload_list(self, selected_id: UUID | None = None) -> None:
        self.card_list.blockSignals(True)
        self.card_list.clear()
        for card in self._cards:
            definition = self._catalog.get(card.metric_id)
            label = card.title or (
                definition.display_name if definition is not None else card.metric_id
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(card.card_id))
            self.card_list.addItem(item)
        self.card_list.blockSignals(False)
        if not self._cards:
            self._selected_card_changed(None, None)
            return
        selected_row = 0
        if selected_id is not None:
            selected_row = next(
                (index for index, card in enumerate(self._cards) if card.card_id == selected_id),
                0,
            )
        self.card_list.setCurrentRow(selected_row)

    def _update_list_item(self, row: int) -> None:
        item = self.card_list.item(row)
        if item is None:
            return
        card = self._cards[row]
        definition = self._catalog[card.metric_id]
        item.setText(card.title or definition.display_name)

    def _normalize_positions(self) -> None:
        self._cards = [
            card.model_copy(update={"position": position})
            for position, card in enumerate(self._cards)
        ]

    def _card_by_id(self, card_id: UUID) -> DashboardCard:
        return next(card for card in self._cards if card.card_id == card_id)

    def _accept(self) -> None:
        self._update_selected()
        self._normalize_positions()
        layout = DashboardLayout(cards=tuple(self._cards))
        try:
            self._result_layout = self.controller.validate_dashboard_layout(layout)
        except (KeyError, RuntimeError, ValueError) as error:
            QMessageBox.warning(self, "Dashboard Layout Is Incomplete", str(error))
            return
        self.accept()


def _refresh_label(seconds: int) -> str:
    if seconds < 60:
        return f"Every {seconds} seconds"
    if seconds % 60 == 0 and seconds < 3600:
        minutes = seconds // 60
        return f"Every {minutes} minute{'s' if minutes != 1 else ''}"
    hours = seconds // 3600
    return f"Every {hours} hour{'s' if hours != 1 else ''}"
