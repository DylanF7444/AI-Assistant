from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any, Literal
from uuid import UUID

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from assistant.command_center import (
    DeliveryMode,
    DestinationBinding,
    DestinationDefinition,
    RetentionPolicy,
    SourceBinding,
    SourceDefinition,
    SourceUseMode,
    WorkArtifact,
)
from assistant.integrations.connections import ConnectionReference
from assistant.scheduling.contracts import EventTrigger, RecurrenceTrigger
from assistant.ui.desktop.controller import DesktopController

_MISSING = object()


def _friendly(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


class _ConfigurationForm(QWidget):
    """Small schema-driven form for reviewed contribution configuration only."""

    def __init__(self) -> None:
        super().__init__()
        self.layout = QFormLayout(self)
        self.layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._fields: dict[str, tuple[str, QWidget, bool]] = {}
        self._schema: dict[str, Any] = {}

    def set_schema(self, schema: dict[str, Any], values: dict[str, Any] | None = None) -> None:
        while self.layout.rowCount():
            self.layout.removeRow(0)
        self._fields.clear()
        self._schema = schema
        values = values or {}
        required = set(schema.get("required", ()))
        properties = schema.get("properties", {})
        for name, raw_property in properties.items():
            if not isinstance(raw_property, dict):
                continue
            property_schema = self._resolve(raw_property)
            is_required = name in required
            current = values.get(name, _MISSING)
            kind, widget = self._widget(property_schema, current, is_required)
            title = str(property_schema.get("title") or _friendly(name))
            if is_required:
                title += " *"
            description = property_schema.get("description")
            if description:
                widget.setToolTip(str(description))
            self.layout.addRow(title, widget)
            self._fields[name] = (kind, widget, is_required)
        if not self._fields:
            empty = QLabel("No additional settings are required.")
            empty.setWordWrap(True)
            self.layout.addRow(empty)

    def values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, (kind, widget, required) in self._fields.items():
            if kind == "integer":
                assert isinstance(widget, QSpinBox)
                result[name] = widget.value()
            elif kind == "boolean":
                assert isinstance(widget, QCheckBox)
                result[name] = widget.isChecked()
            elif kind == "enum":
                assert isinstance(widget, QComboBox)
                result[name] = widget.currentData()
            else:
                assert isinstance(widget, QLineEdit)
                text = widget.text().strip()
                if not text and not required:
                    continue
                if kind == "array":
                    result[name] = [value.strip() for value in text.split(",") if value.strip()]
                else:
                    result[name] = text
        return result

    def _resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            resolved = self._schema.get("$defs", {}).get(reference.rsplit("/", 1)[-1], {})
            if isinstance(resolved, dict):
                return resolved
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        if isinstance(alternatives, list):
            for alternative in alternatives:
                if isinstance(alternative, dict) and alternative.get("type") != "null":
                    return self._resolve(alternative)
        return schema

    @staticmethod
    def _widget(
        schema: dict[str, Any],
        current: Any,
        required: bool,
    ) -> tuple[str, QWidget]:
        default = schema.get("default", _MISSING)
        value = current if current is not _MISSING else default
        choices = schema.get("enum")
        if isinstance(choices, list) and choices:
            widget = QComboBox()
            for choice in choices:
                widget.addItem(_friendly(str(choice)), choice)
            selected = widget.findData(value)
            widget.setCurrentIndex(max(0, selected))
            return "enum", widget
        schema_type = schema.get("type")
        if schema_type == "integer":
            widget = QSpinBox()
            widget.setMinimum(int(schema.get("minimum", -2_147_483_648)))
            widget.setMaximum(int(schema.get("maximum", 2_147_483_647)))
            if value is not _MISSING and value is not None:
                widget.setValue(int(value))
            return "integer", widget
        if schema_type == "boolean":
            widget = QCheckBox()
            if value is not _MISSING and value is not None:
                widget.setChecked(bool(value))
            return "boolean", widget
        widget = QLineEdit()
        if schema_type == "array":
            widget.setPlaceholderText("Comma-separated values")
            if value is not _MISSING and value is not None:
                widget.setText(", ".join(str(item) for item in value))
            return "array", widget
        if value is not _MISSING and value is not None:
            widget.setText(str(value))
        elif required:
            widget.setPlaceholderText("Required")
        return "string", widget


class _BindingDialog(QDialog):
    def __init__(
        self,
        controller: DesktopController,
        kind: Literal["source", "destination"],
        binding: SourceBinding | DestinationBinding | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.kind = kind
        self.original = binding
        self.setWindowTitle(
            f"{'Edit' if binding else 'Add'} {'source' if kind == 'source' else 'destination'}"
        )
        self.setMinimumWidth(560)

        definitions = (
            controller.command_center_source_definitions()
            if kind == "source"
            else controller.command_center_destination_definitions()
        )
        self.definitions = {
            self._definition_id(definition): definition for definition in definitions
        }

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Choose a reviewed connector and configure only the fields declared by it. "
            "Credentials remain in the connection store."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        basics = QFormLayout()
        self.definition_input = QComboBox()
        for definition in definitions:
            self.definition_input.addItem(
                f"{definition.display_name} — {_friendly(definition.provider_id)}",
                self._definition_id(definition),
            )
        self.name_input = QLineEdit()
        self.enabled_input = QCheckBox("Enabled")
        self.enabled_input.setChecked(True)
        self.account_input = QComboBox()
        self.description = QLabel()
        self.description.setWordWrap(True)
        basics.addRow("Type", self.definition_input)
        basics.addRow("Name", self.name_input)
        basics.addRow("Account", self.account_input)
        basics.addRow("State", self.enabled_input)
        basics.addRow("Purpose", self.description)
        layout.addLayout(basics)

        settings_group = QGroupBox("Connector settings")
        settings_layout = QVBoxLayout(settings_group)
        self.configuration_form = _ConfigurationForm()
        settings_layout.addWidget(self.configuration_form)
        layout.addWidget(settings_group)

        self.policy_group = QGroupBox("Use and safety")
        self.policy_layout = QFormLayout(self.policy_group)
        if kind == "source":
            self.mode_inputs: dict[SourceUseMode, QCheckBox] = {}
            modes = QWidget()
            modes_layout = QVBoxLayout(modes)
            modes_layout.setContentsMargins(0, 0, 0, 0)
            for mode in SourceUseMode:
                checkbox = QCheckBox(_friendly(mode.value))
                self.mode_inputs[mode] = checkbox
                modes_layout.addWidget(checkbox)
            self.max_items_input = QSpinBox()
            self.max_items_input.setRange(1, 200)
            self.max_characters_input = QSpinBox()
            self.max_characters_input.setRange(1_000, 500_000)
            self.retention_input = QComboBox()
            for policy in RetentionPolicy:
                self.retention_input.addItem(_friendly(policy.value), policy.value)
            self.policy_layout.addRow("Use for", modes)
            self.policy_layout.addRow("Maximum items", self.max_items_input)
            self.policy_layout.addRow("Maximum characters", self.max_characters_input)
            self.policy_layout.addRow("Retention", self.retention_input)
        else:
            self.delivery_input = QComboBox()
            self.sensitive_input = QCheckBox("Allow reviewed sensitive content")
            self.policy_layout.addRow("Delivery", self.delivery_input)
            self.policy_layout.addRow("Sensitive data", self.sensitive_input)
        layout.addWidget(self.policy_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.definition_input.currentIndexChanged.connect(self._definition_changed)
        if binding is not None:
            binding_type = binding.source_id if kind == "source" else binding.destination_id
            index = self.definition_input.findData(binding_type)
            self.definition_input.setCurrentIndex(max(0, index))
            self.definition_input.setEnabled(False)
        self._definition_changed()
        if binding is not None:
            self._load(binding)

    def value(self) -> SourceBinding | DestinationBinding:
        definition = self._definition()
        connection = self._connection_reference(definition)
        common: dict[str, Any] = {
            "display_name": self.name_input.text().strip(),
            "enabled": self.enabled_input.isChecked(),
            "connection": connection,
            "configuration": self.configuration_form.values(),
        }
        if self.original is not None:
            common.update(
                binding_id=self.original.binding_id,
                created_at=self.original.created_at,
                updated_at=self.original.updated_at,
            )
        if self.kind == "source":
            assert isinstance(definition, SourceDefinition)
            use_modes = tuple(
                mode for mode, checkbox in self.mode_inputs.items() if checkbox.isChecked()
            )
            return SourceBinding(
                source_id=definition.source_id,
                use_modes=use_modes,
                max_items=self.max_items_input.value(),
                max_characters=self.max_characters_input.value(),
                retention=RetentionPolicy(self.retention_input.currentData()),
                **common,
            )
        assert isinstance(definition, DestinationDefinition)
        return DestinationBinding(
            destination_id=definition.destination_id,
            delivery_mode=DeliveryMode(self.delivery_input.currentData()),
            allow_sensitive=self.sensitive_input.isChecked(),
            **common,
        )

    def _definition_changed(self, _index: int = 0) -> None:
        definition = self._definition()
        self.description.setText(definition.description)
        if self.original is None:
            self.name_input.setText(definition.display_name)
        self._load_accounts(definition)
        schema = (
            self.controller.command_center_source_schema(definition.source_id)
            if isinstance(definition, SourceDefinition)
            else self.controller.command_center_destination_schema(definition.destination_id)
        )
        self.configuration_form.set_schema(schema)
        if isinstance(definition, SourceDefinition):
            for mode, checkbox in self.mode_inputs.items():
                supported = mode in definition.supported_modes
                checkbox.setEnabled(supported)
                checkbox.setChecked(supported and mode == SourceUseMode.ON_DEMAND)
            self.max_items_input.setMaximum(definition.max_items)
            self.max_items_input.setValue(min(20, definition.max_items))
            self.max_characters_input.setMaximum(definition.max_characters)
            self.max_characters_input.setValue(min(20_000, definition.max_characters))
        else:
            self.delivery_input.clear()
            for mode in definition.supported_delivery_modes:
                self.delivery_input.addItem(_friendly(mode.value), mode.value)
            self.sensitive_input.setChecked(False)
            self.sensitive_input.setEnabled(definition.accepts_sensitive)

    def _load(self, binding: SourceBinding | DestinationBinding) -> None:
        self.name_input.setText(binding.display_name)
        self.enabled_input.setChecked(binding.enabled)
        definition = self._definition()
        schema = (
            self.controller.command_center_source_schema(definition.source_id)
            if isinstance(definition, SourceDefinition)
            else self.controller.command_center_destination_schema(definition.destination_id)
        )
        self.configuration_form.set_schema(schema, binding.configuration)
        profile_id = str(binding.connection.profile_id) if binding.connection else ""
        account_index = self.account_input.findData(profile_id)
        self.account_input.setCurrentIndex(max(0, account_index))
        if isinstance(binding, SourceBinding):
            for mode, checkbox in self.mode_inputs.items():
                checkbox.setChecked(mode in binding.use_modes)
            self.max_items_input.setValue(binding.max_items)
            self.max_characters_input.setValue(binding.max_characters)
            self.retention_input.setCurrentIndex(
                max(0, self.retention_input.findData(binding.retention.value))
            )
        else:
            self.delivery_input.setCurrentIndex(
                max(0, self.delivery_input.findData(binding.delivery_mode.value))
            )
            self.sensitive_input.setChecked(binding.allow_sensitive)

    def _load_accounts(self, definition: SourceDefinition | DestinationDefinition) -> None:
        self.account_input.clear()
        if not definition.requires_connection:
            self.account_input.addItem("Not required", "")
            self.account_input.setEnabled(False)
            return
        self.account_input.setEnabled(True)
        self.account_input.addItem("Provider default account", "")
        for profile in self.controller.connection_profiles(definition.provider_id):
            state = "" if profile.enabled else " (disabled)"
            account = f" — {profile.account}" if profile.account else ""
            self.account_input.addItem(
                f"{profile.display_name}{account}{state}",
                str(profile.profile_id),
            )

    def _connection_reference(
        self,
        definition: SourceDefinition | DestinationDefinition,
    ) -> ConnectionReference | None:
        if not definition.requires_connection:
            return None
        raw_profile_id = str(self.account_input.currentData() or "")
        return ConnectionReference(
            provider_id=definition.provider_id,
            profile_id=UUID(raw_profile_id) if raw_profile_id else None,
        )

    def _definition(self) -> SourceDefinition | DestinationDefinition:
        return self.definitions[str(self.definition_input.currentData())]

    @staticmethod
    def _definition_id(definition: SourceDefinition | DestinationDefinition) -> str:
        return (
            definition.source_id
            if isinstance(definition, SourceDefinition)
            else definition.destination_id
        )


class _ScheduleBriefDialog(QDialog):
    def __init__(
        self,
        controller: DesktopController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Schedule a work brief")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        note = QLabel(
            "The schedule freezes every enabled Briefing input. Each run saves a bounded "
            "artifact and posts a summary to Inbox. A configured output is queued for review."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.name_input = QLineEdit("Daily work brief")
        self.title_input = QLineEdit("Work Brief")
        self.frequency_input = QComboBox()
        self.frequency_input.addItem("Hourly", "hours")
        self.frequency_input.addItem("Daily", "daily")
        self.frequency_input.addItem("Weekly", "weekly")
        self.frequency_input.addItem("When an observed input changes", "observation")
        self.frequency_input.setCurrentIndex(1)
        self.interval_input = QSpinBox()
        self.interval_input.setRange(1, 168)
        self.interval_input.setValue(1)
        self.time_input = QTimeEdit()
        self.time_input.setDisplayFormat("h:mm AP")
        self.time_input.setTime(QTime(8, 0))
        self.weekday_input = QComboBox()
        for index, label in enumerate(
            ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        ):
            self.weekday_input.addItem(label, index)
        self.retention_input = QSpinBox()
        self.retention_input.setRange(1, 365)
        self.retention_input.setValue(30)
        self.destination_input = QComboBox()
        self.destination_input.addItem("Inbox summary only", "")
        for destination in controller.command_center_destination_bindings():
            if destination.enabled and destination.allow_sensitive:
                self.destination_input.addItem(
                    f"Queue review: {destination.display_name}",
                    str(destination.binding_id),
                )
        sources = controller.command_center_source_bindings()
        enabled_sources = [
            source
            for source in sources
            if source.enabled and SourceUseMode.BRIEFING in source.use_modes
        ]
        source_text = QLabel(
            ", ".join(source.display_name for source in enabled_sources)
            or "No Briefing inputs configured"
        )
        source_text.setWordWrap(True)
        self.observation_input = QComboBox()
        for source in enabled_sources:
            if SourceUseMode.OBSERVATION in source.use_modes:
                self.observation_input.addItem(source.display_name, str(source.binding_id))
        form.addRow("Schedule name", self.name_input)
        form.addRow("Report title", self.title_input)
        form.addRow("Inputs", source_text)
        form.addRow("Frequency", self.frequency_input)
        form.addRow("Every", self.interval_input)
        form.addRow("At", self.time_input)
        form.addRow("Weekday", self.weekday_input)
        form.addRow("Observed input", self.observation_input)
        form.addRow("Keep artifacts (days)", self.retention_input)
        form.addRow("Optional output", self.destination_input)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Create schedule")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, Any]:
        frequency = str(self.frequency_input.currentData())
        selected_time = self.time_input.time()
        local_time = time(selected_time.hour(), selected_time.minute())
        destination_id = str(self.destination_input.currentData() or "")
        if frequency == "observation":
            binding_id = str(self.observation_input.currentData() or "")
            if not binding_id:
                raise ValueError("Enable Observation on a reviewed Gmail or Calendar input")
            trigger = EventTrigger(
                observation_type="integration",
                target=binding_id,
                events=("changed",),
                minimum_interval_seconds=60,
            )
        else:
            trigger = RecurrenceTrigger(
                frequency=frequency,
                interval=self.interval_input.value(),
                local_time=local_time if frequency in {"daily", "weekly"} else None,
                weekdays=(
                    (int(self.weekday_input.currentData()),) if frequency == "weekly" else ()
                ),
                start_at=datetime.now(UTC),
                timezone=self.controller.container.settings.default_timezone,
            )
        return {
            "name": self.name_input.text().strip(),
            "title": self.title_input.text().strip(),
            "trigger": trigger,
            "retention_days": self.retention_input.value(),
            "destination_binding_id": UUID(destination_id) if destination_id else None,
        }


class _ArtifactDialog(QDialog):
    def __init__(self, artifact: WorkArtifact, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.artifact = artifact
        self.setWindowTitle(f"Edit {artifact.title}")
        self.setMinimumSize(680, 520)
        layout = QVBoxLayout(self)
        details = QLabel(
            f"Revision {artifact.revision} · digest {artifact.digest[:12]}… · "
            f"{len(artifact.sources)} source(s)"
        )
        details.setWordWrap(True)
        layout.addWidget(details)
        form = QFormLayout()
        self.title_input = QLineEdit(artifact.title)
        self.body_input = QTextEdit()
        self.body_input.setPlainText(artifact.body)
        self.body_input.setAccessibleName("Reviewed artifact body")
        form.addRow("Title", self.title_input)
        form.addRow("Body", self.body_input)
        layout.addLayout(form, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ConnectionsPage(QWidget):
    """User-facing catalog and binding editor for command-center I/O."""

    extension_hub_requested = Signal(str)
    work_brief_requested = Signal()
    delivery_prepare_requested = Signal(object)

    def __init__(self, controller: DesktopController) -> None:
        super().__init__()
        self.controller = controller
        self.setObjectName("connectionsPage")
        self.setAccessibleName("Command center connections")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        title = QLabel("INPUTS & OUTPUTS")
        title.setObjectName("sectionLabel")
        layout.addWidget(title)
        intro = QLabel(
            "Inputs define the exact places the assistant may read for on-demand work, "
            "briefings, or observation. Outputs define reviewed places where finished work "
            "may be saved, drafted, or sent. Adding a binding never grants permission by itself."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tabs = QTabWidget()
        self.source_table = self._table(("Name", "Input", "Account", "Use", "Status"))
        self.destination_table = self._table(("Name", "Output", "Account", "Delivery", "Status"))
        self.account_table = self._table(("Provider", "Name", "Account", "State"))
        self.artifact_table = self._table(
            ("Title", "Revision", "Sources", "Sensitivity", "Expires")
        )
        self.delivery_table = self._table(("Artifact", "Exact target", "State", "Expires"))
        self.tabs.addTab(
            self._binding_panel("source", self.source_table),
            "Inputs",
        )
        self.tabs.addTab(
            self._binding_panel("destination", self.destination_table),
            "Outputs",
        )
        self.tabs.addTab(self._accounts_panel(), "Accounts")
        self.tabs.addTab(self._work_panel(), "Reviewed work")
        layout.addWidget(self.tabs, 1)
        self.reload()

    def reload(self) -> None:
        sources = self.controller.command_center_source_bindings()
        destinations = self.controller.command_center_destination_bindings()
        source_definitions = {
            definition.source_id: definition
            for definition in self.controller.command_center_source_definitions()
        }
        destination_definitions = {
            definition.destination_id: definition
            for definition in self.controller.command_center_destination_definitions()
        }
        self.source_table.setRowCount(len(sources))
        for row, binding in enumerate(sources):
            definition = source_definitions.get(binding.source_id)
            status = self.controller.command_center_source_status(binding)
            self._set_row(
                self.source_table,
                row,
                (
                    binding.display_name,
                    definition.display_name if definition else binding.source_id,
                    self._account_label(binding.connection),
                    ", ".join(_friendly(mode.value) for mode in binding.use_modes),
                    _friendly(status.availability.value),
                ),
                binding.binding_id,
                definition.provider_id if definition else "",
                status.message,
            )
        self.destination_table.setRowCount(len(destinations))
        for row, binding in enumerate(destinations):
            definition = destination_definitions.get(binding.destination_id)
            status = self.controller.command_center_destination_status(binding)
            self._set_row(
                self.destination_table,
                row,
                (
                    binding.display_name,
                    definition.display_name if definition else binding.destination_id,
                    self._account_label(binding.connection),
                    _friendly(binding.delivery_mode.value),
                    _friendly(status.availability.value),
                ),
                binding.binding_id,
                definition.provider_id if definition else "",
                status.message,
            )
        profiles = self.controller.connection_profiles()
        self.account_table.setRowCount(len(profiles))
        for row, profile in enumerate(profiles):
            self._set_row(
                self.account_table,
                row,
                (
                    _friendly(profile.provider_id),
                    profile.display_name,
                    profile.account or "—",
                    "Enabled" if profile.enabled else "Disabled",
                ),
                profile.profile_id,
                profile.provider_id,
            )
        artifacts = self.controller.command_center_artifacts()
        self.artifact_table.setRowCount(len(artifacts))
        for row, artifact in enumerate(artifacts):
            self._set_row(
                self.artifact_table,
                row,
                (
                    artifact.title,
                    str(artifact.revision),
                    str(len(artifact.sources)),
                    "Sensitive" if artifact.sensitive else "Standard",
                    artifact.expires_at.astimezone().strftime("%Y-%m-%d"),
                ),
                artifact.artifact_id,
                "local",
                f"Digest: {artifact.digest}",
            )
        artifact_titles = {artifact.artifact_id: artifact.title for artifact in artifacts}
        deliveries = self.controller.command_center_pending_deliveries()
        self.delivery_table.setRowCount(len(deliveries))
        for row, delivery in enumerate(deliveries):
            self._set_row(
                self.delivery_table,
                row,
                (
                    artifact_titles.get(delivery.artifact_id, str(delivery.artifact_id)),
                    delivery.target_preview,
                    _friendly(delivery.state.value),
                    delivery.expires_at.astimezone().strftime("%Y-%m-%d %H:%M"),
                ),
                delivery.delivery_id,
                "local",
                delivery.error or f"Artifact revision {delivery.artifact_revision}",
            )

    def _binding_panel(
        self,
        kind: Literal["source", "destination"],
        table: QTableWidget,
    ) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        controls = QHBoxLayout()
        add_button = QPushButton("Add input" if kind == "source" else "Add output")
        edit_button = QPushButton("Edit")
        remove_button = QPushButton("Remove")
        manage_button = QPushButton("Manage accounts")
        refresh_button = QPushButton("Refresh")
        add_button.clicked.connect(lambda: self._add(kind))
        edit_button.clicked.connect(lambda: self._edit(kind))
        remove_button.clicked.connect(lambda: self._remove(kind))
        manage_button.clicked.connect(lambda: self._manage_selected(table))
        refresh_button.clicked.connect(self.reload)
        controls.addWidget(add_button)
        controls.addWidget(edit_button)
        controls.addWidget(remove_button)
        controls.addStretch()
        controls.addWidget(manage_button)
        controls.addWidget(refresh_button)
        layout.addLayout(controls)
        layout.addWidget(table, 1)
        table.doubleClicked.connect(lambda _index: self._edit(kind))
        return panel

    def _accounts_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        note = QLabel(
            "This list contains non-secret account metadata. Connect, reauthorize, or "
            "disconnect provider accounts in Extension Hub."
        )
        note.setWordWrap(True)
        controls = QHBoxLayout()
        manage = QPushButton("Open Extension Hub")
        manage.clicked.connect(lambda: self._manage_selected(self.account_table))
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload)
        controls.addStretch()
        controls.addWidget(manage)
        controls.addWidget(refresh)
        layout.addWidget(note)
        layout.addLayout(controls)
        layout.addWidget(self.account_table, 1)
        return panel

    def _work_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        note = QLabel(
            "A brief stays transient until you explicitly build and save it. Saved work is "
            "revisioned; delivery always rechecks the exact revision and target shown here."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        artifact_label = QLabel("SAVED ARTIFACTS")
        artifact_label.setObjectName("sectionLabel")
        artifact_controls = QHBoxLayout()
        build = QPushButton("Build & save brief")
        schedule = QPushButton("Schedule brief")
        edit = QPushButton("Edit selected")
        queue = QPushButton("Queue delivery")
        remove = QPushButton("Delete")
        build.clicked.connect(self.work_brief_requested.emit)
        schedule.clicked.connect(self._schedule_brief)
        edit.clicked.connect(self._edit_artifact)
        queue.clicked.connect(self._queue_delivery)
        remove.clicked.connect(self._delete_artifact)
        artifact_controls.addWidget(build)
        artifact_controls.addWidget(schedule)
        artifact_controls.addWidget(edit)
        artifact_controls.addWidget(queue)
        artifact_controls.addWidget(remove)
        artifact_controls.addStretch()
        layout.addWidget(artifact_label)
        layout.addLayout(artifact_controls)
        layout.addWidget(self.artifact_table, 1)
        self.artifact_table.doubleClicked.connect(lambda _index: self._edit_artifact())

        delivery_label = QLabel("DELIVERY REVIEW")
        delivery_label.setObjectName("sectionLabel")
        delivery_controls = QHBoxLayout()
        review = QPushButton("Review & continue")
        cancel = QPushButton("Cancel delivery")
        refresh = QPushButton("Refresh")
        review.clicked.connect(self._review_delivery)
        cancel.clicked.connect(self._cancel_delivery)
        refresh.clicked.connect(self.reload)
        delivery_controls.addWidget(review)
        delivery_controls.addWidget(cancel)
        delivery_controls.addStretch()
        delivery_controls.addWidget(refresh)
        layout.addWidget(delivery_label)
        layout.addLayout(delivery_controls)
        layout.addWidget(self.delivery_table, 1)
        privacy_controls = QHBoxLayout()
        clear_observations = QPushButton("Clear observation state")
        clear_saved = QPushButton("Clear saved work")
        clear_observations.clicked.connect(self._clear_observation_data)
        clear_saved.clicked.connect(self._clear_saved_work)
        privacy_controls.addStretch()
        privacy_controls.addWidget(clear_observations)
        privacy_controls.addWidget(clear_saved)
        layout.addLayout(privacy_controls)
        return panel

    def _schedule_brief(self) -> None:
        dialog = _ScheduleBriefDialog(self.controller, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            schedule = self.controller.schedule_command_center_brief(**dialog.values())
        except Exception as exc:
            QMessageBox.warning(self, "Brief not scheduled", str(exc))
            return
        QMessageBox.information(
            self,
            "Work brief scheduled",
            f"{schedule.name} is active. Its next run is shown in Automations.",
        )

    def _clear_observation_data(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear observation state",
            "Clear provider fingerprints and backoff state? The next poll establishes a new "
            "baseline and will not replay old changes.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self.controller.clear_command_center_observation_data()
        QMessageBox.information(
            self,
            "Observation state cleared",
            f"Removed {removed} metadata cursor(s). Connected accounts were not changed.",
        )

    def _clear_saved_work(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear saved work",
            "Permanently delete all command-center artifacts, revisions, and delivery history?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        artifacts, deliveries = self.controller.clear_command_center_artifact_data()
        self.reload()
        QMessageBox.information(
            self,
            "Saved work cleared",
            f"Deleted {artifacts} artifact(s) and {deliveries} delivery record(s). "
            "This cannot be undone.",
        )

    def _edit_artifact(self) -> None:
        artifact_id = self._selected_id(self.artifact_table)
        artifact = next(
            (
                value
                for value in self.controller.command_center_artifacts()
                if value.artifact_id == artifact_id
            ),
            None,
        )
        if artifact is None:
            QMessageBox.information(self, "Reviewed work", "Select an artifact to edit.")
            return
        dialog = _ArtifactDialog(artifact, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.controller.edit_command_center_artifact(
                artifact.artifact_id,
                expected_revision=artifact.revision,
                title=dialog.title_input.text().strip(),
                body=dialog.body_input.toPlainText().strip(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Artifact not saved", str(exc))
            return
        self.reload()

    def _delete_artifact(self) -> None:
        artifact_id = self._selected_id(self.artifact_table)
        if artifact_id is None:
            QMessageBox.information(self, "Reviewed work", "Select an artifact to delete.")
            return
        answer = QMessageBox.question(
            self,
            "Delete artifact",
            "Delete this artifact, all revisions, and its delivery history?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.controller.delete_command_center_artifact(artifact_id)
        self.reload()

    def _queue_delivery(self) -> None:
        artifact_id = self._selected_id(self.artifact_table)
        if artifact_id is None:
            QMessageBox.information(self, "Delivery", "Select an artifact first.")
            return
        destinations = tuple(
            binding
            for binding in self.controller.command_center_destination_bindings()
            if binding.enabled
        )
        if not destinations:
            QMessageBox.information(
                self,
                "Delivery",
                "Add and enable an output before queueing delivery.",
            )
            return
        labels = [binding.display_name for binding in destinations]
        selected, accepted = QInputDialog.getItem(
            self,
            "Queue reviewed delivery",
            "Output",
            labels,
            editable=False,
        )
        if not accepted:
            return
        destination = destinations[labels.index(selected)]
        try:
            pending = self.controller.queue_command_center_delivery(
                artifact_id,
                destination.binding_id,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Delivery not queued", str(exc))
            return
        self.reload()
        QMessageBox.information(
            self,
            "Delivery queued for review",
            f"Exact target: {pending.target_preview}\n\nNothing has been sent yet.",
        )

    def _review_delivery(self) -> None:
        delivery_id = self._selected_id(self.delivery_table)
        delivery = next(
            (
                value
                for value in self.controller.command_center_pending_deliveries()
                if value.delivery_id == delivery_id
            ),
            None,
        )
        if delivery is None:
            QMessageBox.information(self, "Delivery", "Select a pending delivery.")
            return
        answer = QMessageBox.question(
            self,
            "Review exact delivery",
            f"Artifact revision: {delivery.artifact_revision}\n"
            f"Exact target: {delivery.target_preview}\n\n"
            "Continue to permission and confirmation checks?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.delivery_prepare_requested.emit(delivery.delivery_id)

    def _cancel_delivery(self) -> None:
        delivery_id = self._selected_id(self.delivery_table)
        if delivery_id is None:
            QMessageBox.information(self, "Delivery", "Select a delivery to cancel.")
            return
        try:
            self.controller.cancel_command_center_delivery(delivery_id)
        except Exception as exc:
            QMessageBox.warning(self, "Delivery not cancelled", str(exc))
            return
        self.reload()

    def _add(self, kind: Literal["source", "destination"]) -> None:
        self._show_dialog(kind, None)

    def _edit(self, kind: Literal["source", "destination"]) -> None:
        binding = self._selected_binding(kind)
        if binding is None:
            QMessageBox.information(self, "Connections", "Select a binding to edit.")
            return
        self._show_dialog(kind, binding)

    def _show_dialog(
        self,
        kind: Literal["source", "destination"],
        binding: SourceBinding | DestinationBinding | None,
    ) -> None:
        dialog = _BindingDialog(self.controller, kind, binding, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            value = dialog.value()
            if isinstance(value, SourceBinding):
                self.controller.save_command_center_source(value)
            else:
                self.controller.save_command_center_destination(value)
        except Exception as exc:
            QMessageBox.warning(self, "Could not save binding", str(exc))
            return
        self.reload()

    def _remove(self, kind: Literal["source", "destination"]) -> None:
        binding = self._selected_binding(kind)
        if binding is None:
            QMessageBox.information(self, "Connections", "Select a binding to remove.")
            return
        answer = QMessageBox.question(
            self,
            "Remove binding",
            f"Remove “{binding.display_name}”? The provider account will not be deleted.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if isinstance(binding, SourceBinding):
            self.controller.delete_command_center_source(binding.binding_id)
        else:
            self.controller.delete_command_center_destination(binding.binding_id)
        self.reload()

    def _selected_binding(
        self,
        kind: Literal["source", "destination"],
    ) -> SourceBinding | DestinationBinding | None:
        table = self.source_table if kind == "source" else self.destination_table
        binding_id = self._selected_id(table)
        values: tuple[SourceBinding, ...] | tuple[DestinationBinding, ...] = (
            self.controller.command_center_source_bindings()
            if kind == "source"
            else self.controller.command_center_destination_bindings()
        )
        return next((value for value in values if value.binding_id == binding_id), None)

    def _manage_selected(self, table: QTableWidget) -> None:
        item = table.item(table.currentRow(), 0) if table.currentRow() >= 0 else None
        provider_id = str(item.data(Qt.ItemDataRole.UserRole + 1) or "") if item else ""
        self.extension_hub_requested.emit(provider_id)

    def _account_label(self, reference: ConnectionReference | None) -> str:
        if reference is None:
            return "Not required"
        if reference.profile_id is None:
            return "Provider default"
        try:
            profile = next(
                profile
                for profile in self.controller.connection_profiles(reference.provider_id)
                if profile.profile_id == reference.profile_id
            )
        except StopIteration:
            return "Unavailable profile"
        return profile.display_name

    @staticmethod
    def _table(headers: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setStretchLastSection(True)
        for column in range(len(headers) - 1):
            table.horizontalHeader().setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        return table

    @staticmethod
    def _set_row(
        table: QTableWidget,
        row: int,
        values: tuple[str, ...],
        item_id: UUID,
        provider_id: str,
        tooltip: str = "",
    ) -> None:
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if tooltip:
                item.setToolTip(tooltip)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, str(item_id))
                item.setData(Qt.ItemDataRole.UserRole + 1, provider_id)
            table.setItem(row, column, item)

    @staticmethod
    def _selected_id(table: QTableWidget) -> UUID | None:
        row = table.currentRow()
        item = table.item(row, 0) if row >= 0 else None
        if item is None:
            return None
        raw = item.data(Qt.ItemDataRole.UserRole)
        try:
            return UUID(str(raw))
        except (TypeError, ValueError):
            return None
