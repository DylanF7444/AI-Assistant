from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from assistant.audio.activation import parse_voice_prompt_response
from assistant.audio.capture import display_audio_devices
from assistant.bootstrap import Container
from assistant.contracts import (
    PermissionContext,
    PipelineEvent,
    PipelineState,
    Response,
    ResponseType,
    RiskLevel,
    SkillCall,
)
from assistant.diagnostics import hardware_report, model_recommendations
from assistant.execution.engine import ExecutionOutcome
from assistant.integrations.cloud import CloudPricing
from assistant.models import ModelManifest
from assistant.onboarding import OnboardingStore, SetupStep, outcome_guidance, recovery_issues
from assistant.policy.background import background_mode_summary
from assistant.policy.capabilities import available_capabilities
from assistant.policy.permissions import GrantType
from assistant.response import response_from_result
from assistant.scheduling.contracts import ScheduleRunState, ScheduleStatus
from assistant.storage.database import SettingsStore
from assistant.storage.history import HistoryStore
from assistant.storage.macros import MacroDefinition, MacroStep
from assistant.storage.secrets import SecretVault
from assistant.tts.discovery import (
    find_piper_executable,
    find_piper_voice_models,
    piper_executable_dialog_directory,
    piper_model_dialog_directory,
)
from assistant.tts.elevenlabs import ElevenLabsProvider
from assistant.ui.desktop.controller import DesktopController
from assistant.ui.desktop.state import DesktopWindowState, WindowGeometry
from assistant.ui.desktop.tabs import TAB_LABELS
from assistant.windows.adapters import LocalWindowsDeviceAdapter
from assistant.windows.singleton import acquire_single_instance

_WINDOWS_APP_USER_MODEL_ID = "LocalAssistant.Desktop"


def _accessible_stylesheet(
    stylesheet: str,
    *,
    text_scale: float,
    high_contrast: bool,
) -> str:
    """Scale point sizes and optionally add a high-contrast color override."""

    def scale(match: re.Match[str]) -> str:
        points = float(match.group(1)) * text_scale
        rendered = f"{points:.2f}".rstrip("0").rstrip(".")
        return f"font-size: {rendered}pt"

    scaled = re.sub(r"font-size:\s*([0-9]+(?:\.[0-9]+)?)pt", scale, stylesheet)
    if not high_contrast:
        return scaled
    return (
        scaled
        + """
        QMainWindow, QWidget, QDialog, QMessageBox { background: #000000; color: #ffffff; }
        QLabel, QCheckBox, QRadioButton { color: #ffffff; }
        QTextEdit, QLineEdit, QComboBox, QListWidget, QTableWidget {
            background: #000000; color: #ffffff; border: 2px solid #ffffff;
        }
        QTextEdit:focus, QLineEdit:focus, QComboBox:focus, QListWidget:focus,
        QPushButton:focus, QCheckBox:focus { border: 3px solid #00ffff; }
        QPushButton { background: #000000; color: #ffffff; border: 2px solid #ffffff; }
        QPushButton:disabled { color: #a0a0a0; border-color: #777777; }
        QTabBar::tab { color: #ffffff; }
        QTabBar::tab:selected { background: #002b36; color: #ffffff; border-left: 4px solid #00ffff; }
        QProgressBar::chunk { background: #00ffff; }
        """
    )


_DEMO_STRING_VALUES = {
    "application": "notepad",
    "body": "Created by the Skills tab demo.",
    "expression": "2 + 2",
    "from_unit": "mile",
    "label": "Skills tab demo",
    "message": "Skills tab demo",
    "name": "demo",
    "path": ".",
    "query": "demo",
    "root": ".",
    "text": "Skills tab demo",
    "title": "Skills tab demo",
    "to_unit": "kilometer",
    "url": "https://example.com",
}


_SKILL_GROUP_STYLES = {
    "Assistant & Security": ("#b996ff", "#1f1b35"),
    "Connections": ("#4fd7ef", "#122d37"),
    "Files & Folders": ("#72e2bd", "#15342d"),
    "Media": ("#ffb86b", "#382b1c"),
    "Productivity": ("#87aef8", "#172b49"),
    "Visualization": ("#ff86c8", "#351d2d"),
    "Web & Cloud": ("#ffd18a", "#3a2a18"),
    "Windows": ("#ff9cac", "#3a1e2a"),
}
_SKILL_GROUP_FALLBACK_STYLES = (
    ("#9fc0ff", "#172b49"),
    ("#72e2bd", "#15342d"),
    ("#ffd18a", "#3a2a18"),
)


def _group_skill_manifests(manifests: Any) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    """Return manifests grouped by their declared category for the Skills tab."""
    grouped: dict[str, list[Any]] = {}
    for manifest in manifests:
        category = str(getattr(manifest, "category", "General") or "General").strip()
        category = category or "General"
        grouped.setdefault(category, []).append(manifest)
    return tuple(
        (
            category,
            tuple(
                sorted(
                    values,
                    key=lambda item: (
                        str(getattr(item, "display_name", "")).casefold(),
                        str(getattr(item, "skill_id", "")).casefold(),
                    ),
                )
            ),
        )
        for category, values in sorted(grouped.items(), key=lambda item: item[0].casefold())
    )


def _skill_group_style(category: str, group_index: int) -> tuple[str, str]:
    return _SKILL_GROUP_STYLES.get(
        category,
        _SKILL_GROUP_FALLBACK_STYLES[group_index % len(_SKILL_GROUP_FALLBACK_STYLES)],
    )


def _skill_source_label(from_extension_hub: bool) -> str:
    return "Extension Hub" if from_extension_hub else "Built-in"


def _demo_arguments(input_schema: dict[str, Any]) -> dict[str, Any]:
    """Build an editable starting point from a skill's declared JSON schema."""

    def resolve(schema: dict[str, Any]) -> dict[str, Any]:
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.rsplit("/", 1)[-1]
            target = input_schema.get("$defs", {}).get(name)
            if isinstance(target, dict):
                return target
        return schema

    def sample(schema: dict[str, Any], field_name: str = "") -> Any:
        schema = resolve(schema)
        if "const" in schema:
            return schema["const"]
        choices = schema.get("enum")
        if isinstance(choices, list) and choices:
            return choices[0]
        if "default" in schema:
            return schema["default"]
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        if isinstance(alternatives, list):
            usable = [
                item
                for item in alternatives
                if isinstance(item, dict) and item.get("type") != "null"
            ]
            if usable:
                return sample(usable[0], field_name)
        if isinstance(schema.get("allOf"), list) and schema["allOf"]:
            first = schema["allOf"][0]
            if isinstance(first, dict):
                return sample(first, field_name)

        schema_type = schema.get("type")
        if schema_type == "object" or "properties" in schema:
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            return {
                name: sample(properties[name], name)
                for name in required
                if name in properties and isinstance(properties[name], dict)
            }
        if schema_type == "array":
            count = max(0, int(schema.get("minItems", 0)))
            item_schema = schema.get("items", {})
            return [sample(item_schema, field_name) for _ in range(count)]
        if schema_type == "boolean":
            return False
        if schema_type in {"integer", "number"}:
            if "minimum" in schema:
                value = schema["minimum"]
            elif "exclusiveMinimum" in schema:
                value = schema["exclusiveMinimum"] + 1
            else:
                value = 1
            return int(value) if schema_type == "integer" else float(value)
        if schema_type == "string" or not schema_type:
            return _DEMO_STRING_VALUES.get(field_name, "demo")
        return None

    arguments = sample(input_schema)
    return arguments if isinstance(arguments, dict) else {}


def _register_windows_app_identity() -> None:
    if sys.platform != "win32":
        return
    with suppress(Exception):
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_WINDOWS_APP_USER_MODEL_ID)


def _notification_quiet_hours_active(settings: Any, now: datetime | None = None) -> bool:
    if not settings.notification_quiet_hours_enabled:
        return False
    current = (now or datetime.now().astimezone()).time().replace(tzinfo=None)
    start = datetime.strptime(settings.notification_quiet_hours_start, "%H:%M").time()
    end = datetime.strptime(settings.notification_quiet_hours_end, "%H:%M").time()
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def run_desktop(container: Container) -> int:
    instance_mutex = acquire_single_instance()
    controller = DesktopController(container)
    capability_details = {
        "automation.observe.calendar": (
            "Monitor calendar events",
            "Keep an exact calendar event trigger active.",
        ),
        "automation.observe.device": (
            "Monitor a device",
            "Keep an exact device or entity state trigger active.",
        ),
        "automation.observe.file": (
            "Monitor a folder",
            "Keep an exact approved file or folder trigger active.",
        ),
        "automation.observe.process": (
            "Monitor an application",
            "Keep an exact application process trigger active.",
        ),
        "automation.observe.web": (
            "Monitor a web page",
            "Keep an exact bounded web-page change trigger active.",
        ),
        "clipboard.read": ("Read clipboard", "Read the current clipboard contents."),
        "clipboard.write": ("Write clipboard", "Replace the current clipboard contents."),
        "device.control": ("Control devices", "Change or disable connected devices."),
        "device.read": ("Read device information", "Inspect device status and details."),
        "filesystem.read": ("Read files", "Open files or folders for reading."),
        "filesystem.write": ("Change files", "Create, edit, move, rename, or delete files."),
        "filesystem.content.read": (
            "Read document contents",
            "Extract text from explicitly selected documents or approved folders.",
        ),
        "filesystem.content.index": (
            "Index document contents",
            "Retain a searchable local content index for an explicitly approved folder.",
        ),
        "input.control": ("Control mouse and keyboard", "Type, click, or automate input."),
        "integration.read": (
            "Read integration data",
            "Read exact metrics from a configured external application.",
        ),
        "integration.write": (
            "Change integration data",
            "Create, update, move, share, or delete data in a configured external service.",
        ),
        "media.control": ("Control media", "Change playback, volume, or media state."),
        "messaging": ("Send messages", "Send messages through configured accounts."),
        "network": ("Use the network", "Open websites or call network services."),
        "notification.create": ("Show notifications", "Create local system notifications."),
        "process.launch": ("Open applications", "Start registered applications or commands."),
        "process.terminate": ("Stop applications", "Terminate running processes."),
        "process.window": ("Manage windows", "Focus, move, resize, minimize, or close windows."),
        "settings.get": ("Read settings", "Read assistant or system settings."),
        "settings.set": ("Change settings", "Modify assistant or system settings."),
        "system.apps": ("Inspect applications", "List or search installed applications."),
        "system.display": ("Change display", "Adjust display-related system controls."),
        "system.network": ("Change network settings", "Modify Wi-Fi, Bluetooth, or network state."),
        "system.power": ("Power controls", "Sleep, restart, or shut down the system."),
    }

    def friendly_identifier(value: str) -> str:
        return value.replace(".", " ").replace("_", " ").replace("-", " ").title()

    def skill_choice_label(manifest: Any) -> str:
        description = manifest.description.rstrip(".")
        return f"{manifest.display_name} - {description} [{friendly_identifier(manifest.skill_id)}]"

    def capability_name(capability: str) -> str:
        return capability_details.get(capability, (friendly_identifier(capability), ""))[0]

    def capability_description(capability: str) -> str:
        return capability_details.get(capability, ("", "Permission declared by this skill."))[1]

    def capability_table_label(capability: str) -> str:
        return f"{capability_name(capability)} ({capability})"

    def capability_choice_label(capability: str) -> str:
        return f"{capability_name(capability)} - {capability_description(capability)}"

    def target_summary(targets: Any) -> str:
        values = tuple(targets or ())
        if not values or values == ("*",):
            return "Any target"
        return "; ".join(values)

    def grant_type_label(value: str) -> str:
        labels = {
            GrantType.ONCE.value: "Just once",
            GrantType.SESSION.value: "This session",
            GrantType.ALWAYS.value: "Always",
            GrantType.DENY.value: "Denied",
        }
        return labels.get(value, value)

    confirmation_prompt_actions = (
        ("risk:low", "All low-risk actions", False),
        ("risk:medium", "All medium-risk actions", True),
        ("risk:high", "All high-risk actions", True),
        ("risk:critical", "All critical-risk actions", True),
        ("skill:windows.launcher", "Open registered applications", True),
        ("skill:windows.file_operation:open", "Open files", True),
        ("skill:windows.file_operation:copy", "Copy files", True),
        ("skill:windows.file_operation:move", "Move files", True),
        ("skill:windows.file_operation:rename", "Rename files", True),
        ("skill:windows.file_operation:delete", "Delete files", True),
        ("skill:windows.application_window:focus", "Focus application windows", True),
        ("skill:windows.application_window:minimize", "Minimize application windows", True),
        ("skill:windows.application_window:maximize", "Maximize application windows", True),
        ("skill:windows.application_window:close", "Close application windows", True),
        ("skill:windows.process:list", "List processes", True),
        ("skill:windows.process:terminate", "Terminate processes", True),
        ("skill:windows.setting:set", "Change Windows settings", True),
        ("skill:windows.clipboard:read", "Read clipboard", True),
        ("skill:windows.clipboard:write", "Write clipboard", True),
        ("skill:windows.input_automation:type_text", "Type into applications", True),
        ("skill:windows.input_automation:click", "Click in applications", True),
    )

    try:
        from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, Signal
        from PySide6.QtGui import (
            QAction,
            QBrush,
            QColor,
            QIcon,
            QKeySequence,
            QPainter,
            QPixmap,
            QShortcut,
            QTextCursor,
        )
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
            QDialog,
            QFileDialog,
            QFormLayout,
            QFrame,
            QHBoxLayout,
            QHeaderView,
            QInputDialog,
            QLabel,
            QLayout,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMenu,
            QMessageBox,
            QProgressBar,
            QPushButton,
            QScrollArea,
            QSizePolicy,
            QStackedWidget,
            QStyle,
            QStyleOptionTab,
            QStylePainter,
            QSystemTrayIcon,
            QTabBar,
            QTableWidget,
            QTableWidgetItem,
            QTabWidget,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Desktop UI is not installed. Run: python -m pip install -e .[desktop]"
        ) from exc
    from assistant.ui.desktop.dashboard import DashboardPage
    from assistant.ui.desktop.dashboard_connections import DashboardConnectionsDialog
    from assistant.ui.desktop.dashboard_editor import DashboardEditor
    from assistant.ui.desktop.media_viewer import MediaCompactCard, MediaViewer
    from assistant.ui.desktop.notification_widgets import (
        NotificationBanner,
        NotificationDrawer,
    )
    from assistant.ui.desktop.onboarding_dialog import OnboardingDialog
    from assistant.ui.desktop.result_window import ResultWindow
    from assistant.ui.desktop.workers import (
        AsyncWorker,
        PassiveVoiceWorker,
        VoicePromptWorker,
    )

    def application_icon() -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#5a8cff"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(4, 4, 56, 56, 16, 16)
        painter.setPen(QColor("#ffffff"))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(25)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "A")
        painter.end()
        return QIcon(pixmap)

    class RequestTextEdit(QTextEdit):
        submitted = Signal()

        def keyPressEvent(self, event: Any) -> None:
            is_enter = event.key() in (
                Qt.Key.Key_Return,
                Qt.Key.Key_Enter,
            )
            has_shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if is_enter and not has_shift:
                self.submitted.emit()
                event.accept()
                return
            super().keyPressEvent(event)

    class HotkeyBridge(QObject):
        push_to_talk = Signal()
        cancel = Signal()
        pipeline_event = Signal(object)
        notification = Signal(object)
        automation_complete = Signal(object)

    class HorizontalTabBar(QTabBar):
        def tabSizeHint(self, index: int) -> QSize:
            # The labels are painted horizontally even though the bar is on the
            # west edge.  Keep the normal horizontal size hint too; swapping it
            # makes every tab tall and only a few characters wide.
            size = super().tabSizeHint(index)
            text_width = self.fontMetrics().horizontalAdvance(self.tabText(index))
            return QSize(max(size.width(), text_width + 24), size.height())

        def paintEvent(self, event) -> None:
            painter = QStylePainter(self)
            option = QStyleOptionTab()
            for index in range(self.count()):
                self.initStyleOption(option, index)
                option.shape = QTabBar.Shape.RoundedNorth
                painter.drawControl(QStyle.ControlElement.CE_TabBarTab, option)

    class CompactAssistantPanel(QWidget):
        submitted = Signal(str)
        listen_requested = Signal()
        cancel_requested = Signal()
        stop_speaking_requested = Signal()
        expand_requested = Signal()
        pinned_changed = Signal(bool)
        activation_mode_changed = Signal(str)
        inbox_requested = Signal()

        def __init__(self, *, activation_mode: str, pinned: bool, wakeword_active: bool) -> None:
            super().__init__()
            self.setObjectName("compactAssistantPage")
            self.setAccessibleName("Compact assistant companion")
            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 14, 16, 16)
            layout.setSpacing(10)

            header = QHBoxLayout()
            self.status_dot = QLabel("")
            self.status_dot.setObjectName("compactStatusDot")
            self.status_dot.setFixedSize(10, 10)
            self.status = QLabel("Ready")
            self.status.setObjectName("compactStatus")
            self.status.setAccessibleName("Compact assistant status")
            self.pin_button = QPushButton()
            self.pin_button.setObjectName("compactHeaderButton")
            self.pin_button.setCheckable(True)
            self.pin_button.setChecked(pinned)
            self.pin_button.setAccessibleName("Keep compact assistant on top")
            self.pin_button.toggled.connect(self._pin_toggled)
            self._update_pin_label(pinned)
            self.expand_button = QPushButton("Expand")
            self.expand_button.setObjectName("compactHeaderButton")
            self.expand_button.setAccessibleName("Open full assistant")
            self.expand_button.clicked.connect(self.expand_requested.emit)
            self.inbox_button = QPushButton("Inbox")
            self.inbox_button.setObjectName("compactHeaderButton")
            self.inbox_button.setAccessibleName("Open notification inbox")
            self.inbox_button.clicked.connect(self.inbox_requested.emit)
            header.addWidget(self.status_dot)
            header.addWidget(self.status, 1)
            header.addWidget(self.inbox_button)
            header.addWidget(self.pin_button)
            header.addWidget(self.expand_button)
            layout.addLayout(header)

            self.status_detail = QLabel("Waiting for your request")
            self.status_detail.setObjectName("compactStatusDetail")
            self.status_detail.setWordWrap(True)
            self.status_transcript = QLabel("")
            self.status_transcript.setObjectName("compactTranscript")
            self.status_transcript.setWordWrap(True)
            self.status_transcript.setAccessibleName("Compact live speech transcript")
            layout.addWidget(self.status_detail)
            layout.addWidget(self.status_transcript)

            self.pipeline_progress = QProgressBar()
            self.pipeline_progress.setObjectName("compactPipelineProgress")
            self.pipeline_progress.setTextVisible(False)
            self.pipeline_progress.setFixedHeight(4)
            self.pipeline_progress.hide()
            layout.addWidget(self.pipeline_progress)

            voice_row = QHBoxLayout()
            self.activation_mode = QComboBox()
            self.activation_mode.setObjectName("compactActivationMode")
            self.activation_mode.setAccessibleName("Voice activation mode")
            self.activation_mode.addItem("Hotkey", "hotkey")
            self.activation_mode.addItem("Keyword / clap", "keyword_clap")
            self.activation_mode.addItem("Persistent", "persistent")
            selected = self.activation_mode.findData(activation_mode)
            self.activation_mode.setCurrentIndex(max(0, selected))
            self.activation_mode.currentIndexChanged.connect(self._activation_changed)
            self.wakeword_badge = QLabel("WAKE WORD ON" if wakeword_active else "WAKE WORD OFF")
            self.wakeword_badge.setObjectName("wakewordBadge")
            self.wakeword_badge.setProperty("active", wakeword_active)
            self.wakeword_badge.setToolTip(
                "Model-based wake-word monitoring is active."
                if wakeword_active
                else "Configure a wake-word ONNX model in full Settings."
            )
            voice_row.addWidget(self.activation_mode, 1)
            voice_row.addWidget(self.wakeword_badge)
            layout.addLayout(voice_row)

            mic_row = QHBoxLayout()
            mic_label = QLabel("MIC")
            mic_label.setObjectName("sectionLabel")
            self.mic_level = QProgressBar()
            self.mic_level.setObjectName("compactMicLevel")
            self.mic_level.setAccessibleName("Compact microphone input level")
            self.mic_level.setRange(0, 100)
            self.mic_level.setTextVisible(False)
            self.mic_level.setFixedHeight(7)
            self.mic_level_value = QLabel("0%")
            self.mic_level_value.setObjectName("micLevelValue")
            mic_row.addWidget(mic_label)
            mic_row.addWidget(self.mic_level, 1)
            mic_row.addWidget(self.mic_level_value)
            layout.addLayout(mic_row)

            request_label = QLabel("LATEST REQUEST")
            request_label.setObjectName("sectionLabel")
            self.latest_request = QLabel("No request yet.")
            self.latest_request.setObjectName("compactLatestRequest")
            self.latest_request.setWordWrap(True)
            self.latest_request.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            layout.addWidget(request_label)
            layout.addWidget(self.latest_request)

            response_header = QHBoxLayout()
            response_label = QLabel("ASSISTANT")
            response_label.setObjectName("sectionLabel")
            self.response_type = QLabel("READY")
            self.response_type.setObjectName("responseBadge")
            response_header.addWidget(response_label)
            response_header.addStretch()
            response_header.addWidget(self.response_type)
            layout.addLayout(response_header)
            self.response = QTextEdit()
            self.response.setObjectName("compactAssistantResponse")
            self.response.setReadOnly(True)
            self.response.setPlaceholderText("The latest response will appear here.")
            self.response.setAccessibleName("Compact assistant response")
            self.response.setMinimumHeight(110)
            layout.addWidget(self.response, 1)
            self.media_card = MediaCompactCard()
            layout.addWidget(self.media_card)

            compose_label = QLabel("NEW REQUEST")
            compose_label.setObjectName("sectionLabel")
            self.composer = RequestTextEdit()
            self.composer.setObjectName("compactCommandInput")
            self.composer.setAccessibleName("Compact command input")
            self.composer.setPlaceholderText("Ask the assistant...")
            self.composer.setMinimumHeight(58)
            self.composer.setMaximumHeight(86)
            self.composer.submitted.connect(self._submit)
            layout.addWidget(compose_label)
            layout.addWidget(self.composer)

            controls = QHBoxLayout()
            self.listen_button = QPushButton("Listen")
            self.listen_button.setObjectName("secondaryButton")
            self.listen_button.clicked.connect(self.listen_requested.emit)
            self.cancel_button = QPushButton("Stop")
            self.cancel_button.setObjectName("dangerButton")
            self.cancel_button.setAccessibleName("Stop current request")
            self.cancel_button.setEnabled(False)
            self.cancel_button.clicked.connect(self.cancel_requested.emit)
            self.stop_speaking_button = QPushButton("Stop speaking")
            self.stop_speaking_button.setObjectName("secondaryButton")
            self.stop_speaking_button.setAccessibleName("Stop assistant speech")
            self.stop_speaking_button.setEnabled(False)
            self.stop_speaking_button.clicked.connect(self.stop_speaking_requested.emit)
            self.send_button = QPushButton("Send  >")
            self.send_button.setObjectName("primaryButton")
            self.send_button.clicked.connect(self._submit)
            controls.addWidget(self.listen_button)
            controls.addStretch()
            controls.addWidget(self.stop_speaking_button)
            controls.addWidget(self.cancel_button)
            controls.addWidget(self.send_button)
            layout.addLayout(controls)

        def _submit(self) -> None:
            text = self.composer.toPlainText().strip()
            if not text:
                self.composer.setFocus()
                return
            self.submitted.emit(text)

        def _pin_toggled(self, pinned: bool) -> None:
            self._update_pin_label(pinned)
            self.pinned_changed.emit(pinned)

        def _update_pin_label(self, pinned: bool) -> None:
            self.pin_button.setText("Pinned" if pinned else "Pin")

        def _activation_changed(self, _index: int) -> None:
            mode = self.activation_mode.currentData()
            if mode:
                self.activation_mode_changed.emit(str(mode))

        def set_pipeline_busy(
            self,
            *,
            blocked: bool,
            cancellable: bool,
            speech_cancellable: bool = False,
        ) -> None:
            self.composer.setEnabled(not blocked)
            self.send_button.setEnabled(not blocked)
            self.listen_button.setEnabled(not blocked)
            self.activation_mode.setEnabled(not blocked)
            self.expand_button.setEnabled(not blocked)
            self.cancel_button.setEnabled(cancellable)
            self.stop_speaking_button.setEnabled(speech_cancellable)

        def set_request(self, text: str) -> None:
            text = " ".join(text.split())
            self.latest_request.setText(text or "No request yet.")

        def set_response(self, text: str) -> None:
            self.response.setPlainText(text)

        def set_response_type(self, label: str) -> None:
            self.response_type.setText(label)

    class MainWindow(QMainWindow):
        COMPACT_MINIMUM_SIZE = (380, 500)
        COMPACT_DEFAULT_SIZE = (400, 520)
        COMPACT_MAXIMUM_SIZE = (520, 680)
        UNBOUNDED_WIDGET_SIZE = 16_777_215
        ACTIVE_PIPELINE_STATES = {
            PipelineState.LISTENING,
            PipelineState.TRANSCRIBING,
            PipelineState.THINKING,
            PipelineState.EXECUTING,
            PipelineState.SPEAKING,
        }

        @staticmethod
        def _info_label(text: str) -> QLabel:
            label = QLabel(text)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            return label

        @staticmethod
        def _configure_data_table(table: QTableWidget) -> None:
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setAlternatingRowColors(True)
            table.setWordWrap(False)
            header = table.horizontalHeader()
            header.setStretchLastSection(False)
            for column in range(table.columnCount()):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        @staticmethod
        def _table_item(value: Any) -> QTableWidgetItem:
            text = str(value)
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            return item

        def __init__(self) -> None:
            super().__init__()
            self.setObjectName("localAssistantWindow")
            self.setAccessibleName("Local Assistant command window")
            self._workers: list[AsyncWorker] = []
            self._dashboard_workers: list[AsyncWorker] = []
            self._hotkeys: list[int] = []
            self._shortcuts: list[QShortcut] = []
            self._passive_voice_worker: PassiveVoiceWorker | None = None
            self._passive_voice_restart_enabled = False
            self._voice_prompt_active = False
            self._pending_voice_transcript: str | None = None
            self._shutdown_started = False
            self._activity_started = time.perf_counter()
            self._background_correlations: dict[UUID, int] = {}
            self._last_tray_notification_id: str | None = None
            self._status_frame = 0
            self._active_state = PipelineState.IDLE
            self._startup_offline_lock = container.settings.offline_lock
            self._desktop_state_store = SettingsStore(container.database)
            try:
                self._desktop_state = DesktopWindowState.model_validate(
                    self._desktop_state_store.get("desktop_window_state", {})
                )
            except Exception:
                self._desktop_state = DesktopWindowState()
            self._window_mode = self._desktop_state.mode
            self.hotkey_bridge = HotkeyBridge()
            self.hotkey_bridge.push_to_talk.connect(self.listen)
            self.hotkey_bridge.cancel.connect(self.cancel)
            self.hotkey_bridge.pipeline_event.connect(self._handle_pipeline_event)
            self.hotkey_bridge.notification.connect(self._handle_background_notification)
            self.hotkey_bridge.automation_complete.connect(self._handle_automation_completion)
            container.events.add_listener(self.hotkey_bridge.pipeline_event.emit)
            container.notification_center.add_listener(self.hotkey_bridge.notification.emit)
            self.tabs = QTabWidget()
            self.tabs.setObjectName("fullAssistantTabs")
            self.tabs.setTabBar(HorizontalTabBar())
            tab_factories = (
                self._dashboard_tab,
                self._command_tab,
                self._notifications_tab,
                self._active_scripts_tab,
                self._skills_tab,
                self._extension_hub_tab,
                self._connections_tab,
                self._permissions_tab,
                self._file_access_tab,
                self._audit_tab,
                self._settings_tab,
                self._diagnostics_tab,
                self._macros_tab,
                self._playbooks_tab,
                self._models_tab,
                self._knowledge_tab,
                self._memories_tab,
                self._privacy_tab,
                self._applications_tab,
            )
            for factory, label in zip(tab_factories, TAB_LABELS, strict=True):
                self.tabs.addTab(factory(), label)
            self.tabs.setTabPosition(QTabWidget.TabPosition.West)
            self.tabs.setDocumentMode(True)
            self._primary_tab_indices = {
                "dashboard": TAB_LABELS.index("Dashboard"),
                "assistant": TAB_LABELS.index("Assistant"),
            }
            self.tabs.setCurrentIndex(self._primary_tab_indices[self._desktop_state.primary_screen])
            self.tabs.currentChanged.connect(self._full_tab_changed)
            self.compact_panel = CompactAssistantPanel(
                activation_mode=container.settings.voice_activation_mode,
                pinned=self._desktop_state.compact_pinned,
                wakeword_active=container.wakeword is not None,
            )
            self.compact_panel.submitted.connect(self._run_compact_command)
            self.compact_panel.listen_requested.connect(self.listen)
            self.compact_panel.cancel_requested.connect(self.cancel)
            self.compact_panel.stop_speaking_requested.connect(self.stop_speaking)
            self.compact_panel.expand_requested.connect(lambda: self._switch_window_mode("full"))
            self.compact_panel.inbox_requested.connect(self._toggle_notification_drawer)
            self.compact_panel.pinned_changed.connect(self._set_compact_pinned)
            self.compact_panel.activation_mode_changed.connect(self._set_voice_activation_mode)
            self.media_viewer.session_changed.connect(self.compact_panel.media_card.set_snapshot)
            self.compact_panel.media_card.open_requested.connect(self._open_media_viewer)
            self.compact_panel.media_card.close_requested.connect(self.media_viewer.clear)
            self.compact_panel.media_card.play_pause_requested.connect(self.media_viewer.play_pause)
            self.view_stack = QStackedWidget()
            self.view_stack.setObjectName("assistantViewStack")
            self.view_stack.addWidget(self.compact_panel)
            self.view_stack.addWidget(self.tabs)
            self.setCentralWidget(self.view_stack)
            self.notification_banner = NotificationBanner(self)
            self.notification_banner.open_requested.connect(self._open_notification)
            self.notification_drawer = NotificationDrawer(self)
            self.notification_drawer.open_requested.connect(self._open_notification)
            self.notification_drawer.dismiss_requested.connect(self._dismiss_notification)
            self.notification_drawer.see_all_requested.connect(self._open_notification_inbox)
            self._status_timer = QTimer(self)
            self._status_timer.setInterval(420)
            self._status_timer.timeout.connect(self._animate_status)
            self._mic_level_timer = QTimer(self)
            self._mic_level_timer.setInterval(60)
            self._mic_level_timer.timeout.connect(self._update_mic_level)
            self._scripts_timer = QTimer(self)
            self._scripts_timer.setInterval(1000)
            self._scripts_timer.timeout.connect(self._refresh_active_scripts)
            self._scripts_timer.start()
            self._add_shortcut(container.settings.push_to_talk_hotkey, self.listen)
            self._add_shortcut(container.settings.cancel_hotkey, self.cancel)
            self._add_shortcut("Ctrl+1", lambda: self._show_primary_screen("dashboard"))
            self._add_shortcut("Ctrl+2", lambda: self._show_primary_screen("assistant"))
            self._add_shortcut("Ctrl+Return", self._submit_current_view)
            self._register_global_hotkeys()
            self._apply_window_mode(self._window_mode, initial=True)
            self._refresh_notification_ui()
            if container.wakeword:
                self._start_wakeword()
            self._start_passive_voice()
            QTimer.singleShot(0, self._start_retrieval_warmup)
            QTimer.singleShot(0, self._first_run)

        def _start_retrieval_warmup(self) -> None:
            worker = AsyncWorker(container.orchestrator.refresh_retrieval_index())
            worker.failed.connect(
                lambda error: self.statusBar().showMessage(
                    f"Search index warmup was deferred: {error}",
                    3000,
                )
            )
            worker.finished.connect(lambda: self._discard_worker(worker))
            self._workers.append(worker)
            worker.start()

        def _application_settings_payload(self, settings: Any) -> dict[str, Any]:
            return settings.model_dump(
                mode="json",
                exclude={
                    "data_dir",
                    "database_name",
                    "profile",
                    "trusted_model_keys",
                    "trusted_skill_keys",
                },
            )

        def _persist_application_settings(self, settings: Any) -> None:
            SettingsStore(container.database).set(
                "application_settings",
                self._application_settings_payload(settings),
            )
            container.settings = settings
            container.dashboard.set_offline_lock(settings.offline_lock)
            container.extension_hub.set_offline_lock(settings.offline_lock)
            container.extension_hub.set_cloud_pricing(
                CloudPricing.from_mapping(settings.cloud_pricing)
                if settings.cloud_pricing
                else None
            )
            container.dashboard.set_history_retention_days(
                settings.dashboard_history_retention_days
            )

        def _save_current_geometry(self) -> None:
            geometry = self.geometry()
            saved = WindowGeometry(
                x=geometry.x(),
                y=geometry.y(),
                width=geometry.width(),
                height=geometry.height(),
            )
            field = "compact_geometry" if self._window_mode == "compact" else "full_geometry"
            updates: dict[str, Any] = {field: saved}
            if hasattr(self, "result_window"):
                result = self.result_window.normalGeometry()
                updates["result_geometry"] = WindowGeometry(
                    x=result.x(),
                    y=result.y(),
                    width=result.width(),
                    height=result.height(),
                )
            self._desktop_state = self._desktop_state.model_copy(update=updates)

        def _persist_window_state(self) -> None:
            self._desktop_state_store.set(
                "desktop_window_state",
                self._desktop_state.model_dump(mode="json"),
            )

        @staticmethod
        def _available_screen_geometry(target: QRect) -> QRect:
            for screen in QApplication.screens():
                available = screen.availableGeometry()
                if available.intersects(target):
                    return available
            primary = QApplication.primaryScreen()
            return primary.availableGeometry() if primary is not None else QRect(0, 0, 1920, 1080)

        def _bounded_window_geometry(
            self,
            saved: WindowGeometry | None,
            *,
            default_width: int,
            default_height: int,
            minimum_width: int = 1,
            minimum_height: int = 1,
            maximum_width: int | None = None,
            maximum_height: int | None = None,
        ) -> QRect:
            def bounded_size(width: int, height: int, available: QRect) -> tuple[int, int]:
                width_limit = maximum_width or available.width()
                height_limit = maximum_height or available.height()
                bounded_width = max(minimum_width, min(width, width_limit))
                bounded_height = max(minimum_height, min(height, height_limit))
                return (
                    min(bounded_width, available.width()),
                    min(bounded_height, available.height()),
                )

            if saved is None:
                primary = QApplication.primaryScreen()
                available = (
                    primary.availableGeometry() if primary is not None else QRect(0, 0, 1920, 1080)
                )
                width, height = bounded_size(default_width, default_height, available)
                return QRect(
                    max(available.left(), available.right() - width - 24),
                    max(available.top(), available.top() + 48),
                    width,
                    height,
                )
            target = QRect(saved.x, saved.y, saved.width, saved.height)
            available = self._available_screen_geometry(target)
            if not available.intersects(target):
                width, height = bounded_size(default_width, default_height, available)
                return QRect(
                    max(available.left(), available.right() - width - 24),
                    max(available.top(), available.top() + 48),
                    width,
                    height,
                )
            width, height = bounded_size(saved.width, saved.height, available)
            x = min(max(saved.x, available.left()), available.right() - width + 1)
            y = min(max(saved.y, available.top()), available.bottom() - height + 1)
            return QRect(x, y, width, height)

        def _apply_window_mode(self, mode: str, *, initial: bool = False) -> None:
            was_visible = self.isVisible()
            self._window_mode = mode
            compact = mode == "compact"
            self.view_stack.setCurrentWidget(self.compact_panel if compact else self.tabs)
            self.setWindowTitle("Local Assistant - Compact" if compact else "Local Assistant")
            self.setMinimumSize(0, 0)
            self.setMaximumSize(self.UNBOUNDED_WIDGET_SIZE, self.UNBOUNDED_WIDGET_SIZE)
            self.setWindowFlag(
                Qt.WindowType.WindowStaysOnTopHint,
                compact and self._desktop_state.compact_pinned,
            )
            if compact:
                minimum_width, minimum_height = self.COMPACT_MINIMUM_SIZE
                default_width, default_height = self.COMPACT_DEFAULT_SIZE
                maximum_width, maximum_height = self.COMPACT_MAXIMUM_SIZE
                self.setMinimumSize(minimum_width, minimum_height)
                self.setMaximumSize(maximum_width, maximum_height)
                geometry = self._bounded_window_geometry(
                    self._desktop_state.compact_geometry,
                    default_width=default_width,
                    default_height=default_height,
                    minimum_width=minimum_width,
                    minimum_height=minimum_height,
                    maximum_width=maximum_width,
                    maximum_height=maximum_height,
                )
            else:
                self._show_primary_screen(
                    self._desktop_state.primary_screen,
                    persist=False,
                )
                self.setMinimumSize(820, 600)
                geometry = self._bounded_window_geometry(
                    self._desktop_state.full_geometry,
                    default_width=1080,
                    default_height=760,
                )
            self.setGeometry(geometry)
            if was_visible and not initial:
                self.show()
                self.raise_()
                self.activateWindow()
            if compact:
                QTimer.singleShot(0, self.compact_panel.composer.setFocus)

        def _switch_window_mode(self, mode: str) -> None:
            if mode == self._window_mode:
                return
            if self._pipeline_is_busy() or self._voice_prompt_active:
                self.statusBar().showMessage(
                    "Wait for the current assistant interaction to finish.", 1800
                )
                return
            self._save_current_geometry()
            self._desktop_state = self._desktop_state.model_copy(update={"mode": mode})
            self._apply_window_mode(mode)
            self._persist_window_state()

        def _set_compact_pinned(self, pinned: bool) -> None:
            self._desktop_state = self._desktop_state.model_copy(update={"compact_pinned": pinned})
            self._persist_window_state()
            if self._window_mode == "compact":
                self._apply_window_mode("compact")

        def _submit_current_view(self) -> None:
            if self._window_mode == "compact":
                self.compact_panel._submit()
            elif self.tabs.tabText(self.tabs.currentIndex()) == "Assistant":
                self.run_command()

        def _set_voice_activation_mode(self, mode: str) -> None:
            if mode == container.settings.voice_activation_mode:
                return
            try:
                updated = container.settings.model_copy(update={"voice_activation_mode": mode})
                updated = type(container.settings).model_validate(updated.model_dump())
            except Exception as error:
                QMessageBox.critical(self, "Invalid Voice Activation Mode", str(error))
                return
            self._persist_application_settings(updated)
            self._start_passive_voice()
            self._set_pipeline_state(PipelineState.IDLE, self._passive_voice_status())
            self._apply_passive_voice_indicators()

        def show_from_tray(self) -> None:
            if self._window_mode == "full":
                self._show_primary_screen(
                    self._desktop_state.primary_screen,
                    persist=False,
                )
            if self.isMinimized():
                self.showNormal()
            else:
                self.show()
            self.raise_()
            self.activateWindow()

        def open_primary_from_tray(self, screen: str) -> None:
            if self._window_mode != "full":
                self._switch_window_mode("full")
            if self._window_mode == "full":
                self._show_primary_screen(screen)
            self.show_from_tray()

        def open_dashboard_from_tray(self) -> None:
            self.open_primary_from_tray("dashboard")

        def open_assistant_from_tray(self) -> None:
            self.open_primary_from_tray("assistant")

        def open_full_from_tray(self) -> None:
            """Backward-compatible entry point for callers that open the assistant."""
            self.open_assistant_from_tray()

        def _dashboard_tab(self) -> QWidget:
            self.dashboard_page = DashboardPage(controller)
            self.dashboard_page.refresh_requested.connect(self._refresh_dashboard)
            self.dashboard_page.refresh_due_requested.connect(self._refresh_dashboard)
            self.dashboard_page.customize_requested.connect(self._customize_dashboard)
            self.dashboard_page.connections_requested.connect(self._manage_dashboard_connections)
            self.dashboard_page.work_brief_requested.connect(self._run_work_brief)
            self.dashboard_page.permission_requested.connect(self._grant_dashboard_permission)
            self.dashboard_page.became_visible.connect(self._refresh_dashboard)
            return self.dashboard_page

        def _run_work_brief(self) -> None:
            self._show_primary_screen("assistant")
            self._run_text_command("brief me")

        def _manage_dashboard_connections(self) -> None:
            dialog = DashboardConnectionsDialog(controller, self)
            dialog.add_requested.connect(
                lambda payload: self._add_dashboard_connection(dialog, payload)
            )
            dialog.test_requested.connect(
                lambda connection_id: self._test_dashboard_connection(
                    dialog,
                    connection_id,
                )
            )
            dialog.enabled_requested.connect(
                lambda payload: self._set_dashboard_connection_enabled(
                    dialog,
                    payload,
                )
            )
            dialog.history_requested.connect(
                lambda payload: self._set_dashboard_connection_history(
                    dialog,
                    payload,
                )
            )
            dialog.clear_history_requested.connect(
                lambda connection_id: self._clear_dashboard_connection_history(
                    dialog,
                    connection_id,
                )
            )
            dialog.remove_requested.connect(
                lambda connection_id: self._remove_dashboard_connection(
                    dialog,
                    connection_id,
                )
            )
            dialog.exec()

        def _add_dashboard_connection(
            self,
            dialog: DashboardConnectionsDialog,
            payload: dict[str, Any],
        ) -> None:
            worker = AsyncWorker(controller.add_home_assistant_connection(**payload))
            worker.complete.connect(dialog.connection_added)
            worker.failed.connect(dialog.operation_failed)
            worker.finished.connect(lambda: self._discard_dashboard_worker(worker))
            self._workers.append(worker)
            self._dashboard_workers.append(worker)
            worker.start()

        def _test_dashboard_connection(
            self,
            dialog: DashboardConnectionsDialog,
            connection_id: UUID,
        ) -> None:
            worker = AsyncWorker(controller.test_dashboard_connection(connection_id))
            worker.complete.connect(dialog.health_received)
            worker.failed.connect(dialog.operation_failed)
            worker.finished.connect(lambda: self._discard_dashboard_worker(worker))
            self._workers.append(worker)
            self._dashboard_workers.append(worker)
            worker.start()

        def _set_dashboard_connection_enabled(
            self,
            dialog: DashboardConnectionsDialog,
            payload: tuple[UUID, bool],
        ) -> None:
            try:
                connection = controller.set_dashboard_connection_enabled(*payload)
            except (KeyError, RuntimeError, ValueError) as error:
                dialog.operation_failed(str(error))
                return
            dialog.connection_changed(connection)
            self.dashboard_page.reload_layout()
            self._refresh_dashboard()

        def _remove_dashboard_connection(
            self,
            dialog: DashboardConnectionsDialog,
            connection_id: UUID,
        ) -> None:
            try:
                controller.delete_dashboard_connection(connection_id)
            except (KeyError, RuntimeError, ValueError) as error:
                dialog.operation_failed(str(error))
                return
            dialog.connection_changed()
            self.dashboard_page.reload_layout()
            self._refresh_dashboard()

        def _set_dashboard_connection_history(
            self,
            dialog: DashboardConnectionsDialog,
            payload: tuple[UUID, bool],
        ) -> None:
            try:
                connection = controller.set_dashboard_connection_history_enabled(*payload)
            except (KeyError, RuntimeError, ValueError) as error:
                dialog.operation_failed(str(error))
                return
            dialog.connection_changed(connection)

        def _clear_dashboard_connection_history(
            self,
            dialog: DashboardConnectionsDialog,
            connection_id: UUID,
        ) -> None:
            try:
                deleted = controller.clear_dashboard_connection_history(connection_id)
            except (KeyError, RuntimeError, ValueError) as error:
                dialog.operation_failed(str(error))
                return
            dialog.connection_changed(
                next(
                    (
                        connection
                        for connection in controller.dashboard_connections()
                        if connection.connection_id == connection_id
                    ),
                    None,
                )
            )
            dialog.operation_status.setText(f"Deleted {deleted} retained numeric sample(s).")

        def _grant_dashboard_permission(self, card_id: UUID) -> None:
            try:
                request = controller.dashboard_permission_request(card_id)
            except (KeyError, ValueError) as error:
                QMessageBox.critical(self, "Dashboard Permission Error", str(error))
                return
            details = "\n".join(
                f"{capability_name(scope.capability)}: {scope.reason}\n"
                f"Targets: {target_summary(scope.targets)}"
                for scope in request.scopes
            )
            decision = self._voice_prompt_choice(
                title="Dashboard Permission Required",
                text=f"{request.display_name} needs read access before it can refresh.",
                informative_text=details,
                kind="approval",
                buttons=[
                    ("approve", "Grant Access", QMessageBox.ButtonRole.AcceptRole),
                    ("deny", "Not Now", QMessageBox.ButtonRole.RejectRole),
                ],
                default_choice="deny",
            )
            if decision != "approve":
                return
            modes = {
                "once": GrantType.ONCE,
                "session": GrantType.SESSION,
                "always": GrantType.ALWAYS,
            }
            mode = self._voice_prompt_choice(
                title="Grant Dashboard Access",
                text="How long should this read-only dashboard access last?",
                informative_text=(
                    "The grant is limited to the exact targets shown in the previous step."
                ),
                kind="permission_duration",
                buttons=[
                    ("once", "Just once", QMessageBox.ButtonRole.AcceptRole),
                    ("session", "This session", QMessageBox.ButtonRole.AcceptRole),
                    ("always", "Always", QMessageBox.ButtonRole.AcceptRole),
                    ("deny", "Cancel", QMessageBox.ButtonRole.RejectRole),
                ],
                default_choice="deny",
            )
            if mode not in modes:
                return
            try:
                controller.grant_dashboard_permission(card_id, modes[mode])
            except (KeyError, RuntimeError, ValueError) as error:
                QMessageBox.critical(self, "Dashboard Permission Error", str(error))
                return
            self._reload_permissions()
            self.statusBar().showMessage(
                f"Access granted for {request.display_name}.",
                4000,
            )
            self._refresh_dashboard()

        def _customize_dashboard(self) -> None:
            dialog = DashboardEditor(controller, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                controller.save_dashboard_layout(dialog.selected_layout())
            except (KeyError, ValueError) as error:
                QMessageBox.critical(self, "Dashboard Layout Not Saved", str(error))
                return
            self.dashboard_page.reload_layout()
            self._refresh_dashboard()

        def _refresh_dashboard(
            self,
            card_ids: tuple[UUID, ...] | None = None,
        ) -> None:
            if isinstance(card_ids, bool):
                card_ids = None
            if not self.dashboard_page.begin_refresh(card_ids):
                return
            worker = AsyncWorker(controller.refresh_dashboard(card_ids))
            worker.complete.connect(self.dashboard_page.apply_snapshots)
            worker.failed.connect(self.dashboard_page.fail_refresh)
            worker.finished.connect(lambda: self._discard_dashboard_worker(worker))
            self._workers.append(worker)
            self._dashboard_workers.append(worker)
            worker.start()

        def _full_tab_changed(self, index: int) -> None:
            label = self.tabs.tabText(index)
            if label == "Connections":
                self.connections_page.reload()
            screen = {
                "Dashboard": "dashboard",
                "Assistant": "assistant",
            }.get(label)
            if screen is None:
                return
            if self._desktop_state.primary_screen != screen:
                self._desktop_state = self._desktop_state.model_copy(
                    update={"primary_screen": screen}
                )
                self._persist_window_state()
            if screen == "assistant":
                QTimer.singleShot(0, self.transcript.setFocus)

        def _show_primary_screen(self, screen: str, *, persist: bool = True) -> None:
            index = self._primary_tab_indices[screen]
            self.tabs.setCurrentIndex(index)
            if persist and self._desktop_state.primary_screen != screen:
                self._desktop_state = self._desktop_state.model_copy(
                    update={"primary_screen": screen}
                )
                self._persist_window_state()
            if screen == "dashboard":
                QTimer.singleShot(0, self._refresh_dashboard)
            else:
                QTimer.singleShot(0, self.transcript.setFocus)

        def _notifications_tab(self) -> QWidget:
            page = QWidget()
            page.setObjectName("notificationsPage")
            layout = QVBoxLayout(page)
            layout.setContentsMargins(28, 24, 28, 24)
            layout.setSpacing(12)

            header = QHBoxLayout()
            heading = QVBoxLayout()
            title = QLabel("Inbox")
            title.setObjectName("pageTitle")
            subtitle = QLabel(
                "Reminders, completed work, reports, and items that need your attention."
            )
            subtitle.setObjectName("pageSubtitle")
            subtitle.setWordWrap(True)
            heading.addWidget(title)
            heading.addWidget(subtitle)
            mark_all = QPushButton("Mark all read")
            mark_all.clicked.connect(self._mark_all_notifications_read)
            refresh = QPushButton("Refresh")
            refresh.clicked.connect(self._refresh_notification_ui)
            header.addLayout(heading, 1)
            header.addWidget(mark_all)
            header.addWidget(refresh)
            layout.addLayout(header)

            self.notification_table = QTableWidget(0, 5)
            self.notification_table.setObjectName("notificationTable")
            self.notification_table.setAccessibleName("Notification inbox")
            self.notification_table.setHorizontalHeaderLabels(
                ["Status", "Time", "Type", "Title", "Preview"]
            )
            self._configure_data_table(self.notification_table)
            self.notification_table.horizontalHeader().setSectionResizeMode(
                4, QHeaderView.ResizeMode.Stretch
            )
            self.notification_table.itemSelectionChanged.connect(self._update_notification_detail)
            self.notification_table.cellDoubleClicked.connect(
                lambda _row, _column: self._open_selected_notification()
            )
            layout.addWidget(self.notification_table, 1)

            self.notification_detail = QTextEdit()
            self.notification_detail.setObjectName("notificationDetail")
            self.notification_detail.setAccessibleName("Selected notification details")
            self.notification_detail.setReadOnly(True)
            self.notification_detail.setPlaceholderText(
                "Select a notification to see its full preview."
            )
            self.notification_detail.setMaximumHeight(150)
            layout.addWidget(self.notification_detail)

            actions = QHBoxLayout()
            open_selected = QPushButton("Open result")
            open_selected.setObjectName("primaryButton")
            open_selected.clicked.connect(self._open_selected_notification)
            mark_read = QPushButton("Mark read")
            mark_read.clicked.connect(self._mark_selected_notification_read)
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self._dismiss_selected_notification)
            actions.addWidget(open_selected)
            actions.addWidget(mark_read)
            actions.addWidget(dismiss)
            actions.addStretch()
            layout.addLayout(actions)
            return page

        def _refresh_notification_ui(self) -> None:
            notifications = container.notification_center.notifications(limit=200)
            unread_count = container.notification_center.unread_count()
            if hasattr(self, "notification_table"):
                selected_id = self._selected_table_id(self.notification_table)
                self.notification_table.setRowCount(0)
                for row, notification in enumerate(notifications):
                    self._set_table_row(
                        self.notification_table,
                        row,
                        [
                            "New" if notification.read_at is None else "Read",
                            notification.created_at.astimezone().strftime("%b %d, %I:%M %p"),
                            notification.category.replace("_", " ").title(),
                            notification.title,
                            notification.message,
                        ],
                    )
                    id_item = self.notification_table.item(row, 0)
                    id_item.setData(
                        Qt.ItemDataRole.UserRole,
                        str(notification.notification_id),
                    )
                    if selected_id == str(notification.notification_id):
                        self.notification_table.selectRow(row)
                self._update_notification_detail()
            if hasattr(self, "notification_drawer"):
                self.notification_drawer.refresh(
                    notifications[:8],
                    unread_count=unread_count,
                )
            badge = "99+" if unread_count > 99 else str(unread_count)
            label = f"Inbox ({badge})" if unread_count else "Inbox"
            if hasattr(self, "tabs"):
                self.tabs.setTabText(TAB_LABELS.index("Inbox"), label)
            if hasattr(self, "notification_button"):
                self.notification_button.setText(label)
            if hasattr(self, "compact_panel"):
                self.compact_panel.inbox_button.setText(label)

        def _selected_notification_id(self) -> UUID | None:
            if not hasattr(self, "notification_table"):
                return None
            value = self._selected_table_id(self.notification_table)
            try:
                return UUID(value) if value else None
            except ValueError:
                return None

        def _update_notification_detail(self) -> None:
            if not hasattr(self, "notification_detail"):
                return
            notification_id = self._selected_notification_id()
            if notification_id is None:
                self.notification_detail.clear()
                return
            try:
                notification = container.notification_center.get(notification_id)
            except KeyError:
                self.notification_detail.clear()
                return
            self.notification_detail.setPlainText(f"{notification.title}\n\n{notification.message}")

        def _mark_selected_notification_read(self) -> None:
            notification_id = self._selected_notification_id()
            if notification_id is None:
                self.statusBar().showMessage("Select a notification first.", 3000)
                return
            with suppress(KeyError):
                container.notification_center.mark_read(notification_id)
            self._refresh_notification_ui()

        def _mark_all_notifications_read(self) -> None:
            changed = container.notification_center.mark_all_read()
            self.statusBar().showMessage(
                f"Marked {changed} notification{'s' if changed != 1 else ''} as read.",
                3000,
            )
            self._refresh_notification_ui()

        def _dismiss_selected_notification(self) -> None:
            notification_id = self._selected_notification_id()
            if notification_id is None:
                self.statusBar().showMessage("Select a notification first.", 3000)
                return
            self._dismiss_notification(str(notification_id))

        def _dismiss_notification(self, notification_id: str) -> None:
            with suppress(KeyError, ValueError):
                container.notification_center.dismiss(UUID(notification_id))
            self._refresh_notification_ui()

        def _open_selected_notification(self) -> None:
            notification_id = self._selected_notification_id()
            if notification_id is None:
                self.statusBar().showMessage("Select a notification first.", 3000)
                return
            self._open_notification(str(notification_id))

        def _open_notification(self, notification_id: str) -> None:
            try:
                notification = container.notification_center.mark_read(UUID(notification_id))
            except (KeyError, ValueError):
                self._refresh_notification_ui()
                return
            self.notification_drawer.hide()
            if notification.run_id is not None:
                self._open_automation_run(notification.run_id)
            elif notification.category == "media":
                self._open_media_viewer()
            else:
                self._open_notification_inbox()
            self._refresh_notification_ui()

        def _open_automation_run(self, run_id: UUID) -> None:
            try:
                run = container.schedule_store.get_run(run_id)
            except KeyError:
                self._show_automation_run(run_id)
                return
            if isinstance(run.result, dict):
                schedule = next(
                    (
                        item
                        for item in container.schedule_store.list()
                        if item.schedule_id == run.schedule_id
                    ),
                    None,
                )
                display = run.result.get("display_text")
                summary = run.result.get("summary")
                sections = []
                if isinstance(display, str) and display.strip():
                    sections.append(display.strip())
                if isinstance(summary, str) and summary.strip() and summary.strip() not in sections:
                    sections.append(summary.strip())
                if not sections:
                    sections.append(json.dumps(run.result, indent=2, default=str))
                completed = run.completed_at or run.started_at or run.scheduled_for
                metadata = (
                    f"{completed.astimezone().strftime('%b %d, %Y at %I:%M %p')}"
                    f"  ·  {run.state.value.replace('_', ' ').title()}"
                )
                self.result_window.show_report(
                    title=schedule.name if schedule is not None else "Automation result",
                    body="\n\n".join(sections),
                    metadata=metadata,
                    status=run.result_status or run.state.value,
                    run_id=str(run_id),
                )
                return
            self._show_automation_run(run_id)

        def _show_automation_run(self, run_id: UUID) -> None:
            if self._window_mode != "full":
                self._switch_window_mode("full")
            if self._window_mode != "full":
                return
            self.tabs.setCurrentIndex(TAB_LABELS.index("Automations"))
            self._refresh_active_scripts()
            for table in (self.pending_approvals, self.schedule_history):
                for row in range(table.rowCount()):
                    item = table.item(row, 0)
                    if item is not None and item.data(Qt.ItemDataRole.UserRole) == str(run_id):
                        table.selectRow(row)
                        table.scrollToItem(item)
                        return

        def _open_notification_inbox(self) -> None:
            self.notification_drawer.hide()
            if self._window_mode != "full":
                self._switch_window_mode("full")
            if self._window_mode == "full":
                self.tabs.setCurrentIndex(TAB_LABELS.index("Inbox"))
                self._refresh_notification_ui()

        def _toggle_notification_drawer(self) -> None:
            if self.notification_drawer.isVisible():
                self.notification_drawer.hide()
                return
            self._refresh_notification_ui()
            self._position_notification_overlays()
            self.notification_drawer.show()
            self.notification_drawer.raise_()
            self.notification_drawer.setFocus()

        def _active_scripts_tab(self) -> QWidget:
            page = QWidget()
            page.setObjectName("activeScriptsPage")
            page.setSizePolicy(
                QSizePolicy.Policy.Preferred,
                QSizePolicy.Policy.Preferred,
            )
            layout = QVBoxLayout(page)
            layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
            layout.setContentsMargins(28, 24, 28, 24)
            layout.setSpacing(12)

            header = QHBoxLayout()
            heading = QVBoxLayout()
            title = QLabel("Automations")
            title.setObjectName("pageTitle")
            subtitle = QLabel(
                "Live jobs, durable schedules, pending approvals, and runtime controls."
            )
            subtitle.setObjectName("pageSubtitle")
            heading.addWidget(title)
            heading.addWidget(subtitle)
            refresh = QPushButton("Refresh")
            refresh.clicked.connect(self._refresh_active_scripts)
            self.new_work_brief_automation = QPushButton("New work brief")
            self.new_work_brief_automation.clicked.connect(self._create_work_brief_automation)
            self.pause_automations = QPushButton("Pause all")
            self.pause_automations.clicked.connect(self._toggle_automation_pause)
            header.addLayout(heading, 1)
            header.addWidget(self.new_work_brief_automation)
            header.addWidget(self.pause_automations)
            header.addWidget(refresh)
            layout.addLayout(header)

            running_label = QLabel("RUNNING NOW")
            running_label.setObjectName("sectionLabel")
            self.running_scripts = QTableWidget(0, 5)
            self.running_scripts.setHorizontalHeaderLabels(
                ["Type", "Name", "Details", "Started", "Elapsed"]
            )
            self.running_scripts.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.running_scripts.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.running_scripts.setAlternatingRowColors(True)
            self._configure_automation_table(self.running_scripts, stretch_columns=(1, 2))
            cancel = QPushButton("Cancel selected")
            cancel.setObjectName("dangerButton")
            cancel.clicked.connect(self._cancel_selected_script)

            scheduled_label = QLabel("SCHEDULED")
            scheduled_label.setObjectName("sectionLabel")
            self.scheduled_scripts = QTableWidget(0, 5)
            self.scheduled_scripts.setHorizontalHeaderLabels(
                ["Type", "Name", "Due", "Status", "Risk"]
            )
            self.scheduled_scripts.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.scheduled_scripts.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.scheduled_scripts.setAlternatingRowColors(True)
            self._configure_automation_table(self.scheduled_scripts, stretch_columns=(1,))
            schedule_controls = QHBoxLayout()
            self.schedule_pause_resume = QPushButton("Pause / resume")
            self.schedule_pause_resume.clicked.connect(self._pause_or_resume_schedule)
            schedule_run_now = QPushButton("Run now")
            schedule_run_now.clicked.connect(self._run_selected_schedule)
            schedule_revoke = QPushButton("Revoke authority")
            schedule_revoke.clicked.connect(self._revoke_selected_schedule)
            schedule_delete = QPushButton("Delete")
            schedule_delete.setObjectName("dangerButton")
            schedule_delete.clicked.connect(self._delete_selected_schedule)
            for button in (
                self.schedule_pause_resume,
                schedule_run_now,
                schedule_revoke,
                schedule_delete,
            ):
                schedule_controls.addWidget(button)
            schedule_controls.addStretch(1)

            approvals_label = QLabel("PENDING APPROVAL")
            approvals_label.setObjectName("sectionLabel")
            self.pending_approvals = QTableWidget(0, 5)
            self.pending_approvals.setHorizontalHeaderLabels(
                ["Schedule", "Action", "Risk", "Expires", "Reason"]
            )
            self.pending_approvals.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.pending_approvals.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.pending_approvals.setAlternatingRowColors(True)
            self._configure_automation_table(self.pending_approvals, stretch_columns=(0, 4))
            approval_controls = QHBoxLayout()
            approve = QPushButton("Review and approve")
            approve.clicked.connect(self._review_selected_approval)
            reject = QPushButton("Reject")
            reject.setObjectName("dangerButton")
            reject.clicked.connect(self._reject_selected_approval)
            approval_controls.addWidget(approve)
            approval_controls.addWidget(reject)
            approval_controls.addStretch(1)

            history_label = QLabel("RECENT RUNS")
            history_label.setObjectName("sectionLabel")
            self.schedule_history = QTableWidget(0, 5)
            self.schedule_history.setHorizontalHeaderLabels(
                ["Schedule", "Scheduled", "State", "Attempt", "Details"]
            )
            self.schedule_history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.schedule_history.setAlternatingRowColors(True)
            self._configure_automation_table(self.schedule_history, stretch_columns=(0, 4))
            clear_history = QPushButton("Clear completed history")
            clear_history.clicked.connect(self._clear_completed_schedule_history)

            hooks_label = QLabel("HOOKS")
            hooks_label.setObjectName("sectionLabel")
            self.script_hooks = QTableWidget(0, 3)
            self.script_hooks.setHorizontalHeaderLabels(["Hook", "Trigger", "Status"])
            self.script_hooks.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.script_hooks.setAlternatingRowColors(True)
            self._configure_automation_table(self.script_hooks, stretch_columns=(1,))

            layout.addWidget(running_label)
            layout.addWidget(self.running_scripts)
            layout.addWidget(cancel)
            layout.addWidget(scheduled_label)
            layout.addWidget(self.scheduled_scripts)
            layout.addLayout(schedule_controls)
            layout.addWidget(approvals_label)
            layout.addWidget(self.pending_approvals)
            layout.addLayout(approval_controls)
            layout.addWidget(history_label)
            layout.addWidget(self.schedule_history)
            layout.addWidget(clear_history)
            layout.addWidget(hooks_label)
            layout.addWidget(self.script_hooks)
            self._refresh_active_scripts()

            scroll = QScrollArea()
            scroll.setObjectName("activeScriptsScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            self.automations_scroll = scroll
            return scroll

        @staticmethod
        def _configure_automation_table(
            table: QTableWidget,
            *,
            stretch_columns: tuple[int, ...],
        ) -> None:
            """Keep automation rows readable while allowing the page to scroll."""
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.setWordWrap(False)
            table.setTextElideMode(Qt.TextElideMode.ElideRight)
            table.setMinimumHeight(112)
            table.verticalHeader().setDefaultSectionSize(32)
            header = table.horizontalHeader()
            header.setStretchLastSection(False)
            stretch = set(stretch_columns)
            for column in range(table.columnCount()):
                header.setSectionResizeMode(
                    column,
                    (
                        QHeaderView.ResizeMode.Stretch
                        if column in stretch
                        else QHeaderView.ResizeMode.ResizeToContents
                    ),
                )

        @staticmethod
        def _set_table_row(table: QTableWidget, row: int, values: list[str]) -> None:
            table.insertRow(row)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setStatusTip(value)
                table.setItem(row, column, item)

        def _refresh_active_scripts(self) -> None:
            if not hasattr(self, "running_scripts"):
                return
            selected_id = None
            selected = self.running_scripts.currentRow()
            if selected >= 0:
                item = self.running_scripts.item(selected, 0)
                selected_id = item.data(Qt.ItemDataRole.UserRole) if item else None
            selected_schedule_id = self._selected_table_id(self.scheduled_scripts)
            selected_run_id = self._selected_table_id(self.pending_approvals)
            self.pause_automations.setText(
                "Resume all" if container.scheduler.paused else "Pause all"
            )

            self.running_scripts.setRowCount(0)
            now = datetime.now(UTC)
            for row, job in enumerate(container.engine.active_jobs()):
                is_timer = job.skill_id == "core.timer"
                if is_timer:
                    duration = float(job.arguments.get("seconds", 0))
                    remaining = max(0.0, duration - job.elapsed_seconds)
                    details = f"{remaining:.1f}s remaining of {duration:g}s"
                else:
                    details = json.dumps(job.arguments, default=str)
                self._set_table_row(
                    self.running_scripts,
                    row,
                    [
                        "Timer" if is_timer else "Action",
                        str(job.arguments.get("label", job.display_name)),
                        details,
                        job.started_at.astimezone().strftime("%b %d, %I:%M:%S %p"),
                        f"{job.elapsed_seconds:.1f}s",
                    ],
                )
                id_item = self.running_scripts.item(row, 0)
                id_item.setData(Qt.ItemDataRole.UserRole, str(job.correlation_id))
                if selected_id == str(job.correlation_id):
                    self.running_scripts.selectRow(row)

            self.scheduled_scripts.setRowCount(0)
            schedules = container.schedule_store.list(
                statuses=(
                    ScheduleStatus.ACTIVE,
                    ScheduleStatus.PAUSED,
                    ScheduleStatus.PENDING_AUTHORIZATION,
                    ScheduleStatus.PENDING_REVIEW,
                    ScheduleStatus.COMPLETED,
                    ScheduleStatus.DISABLED,
                )
            )
            for row, schedule in enumerate(schedules):
                due = schedule.next_run_at
                if due is None:
                    due_text = "No due time"
                    status = schedule.status.value.replace("_", " ").title()
                else:
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=UTC)
                    due_text = due.astimezone().strftime("%b %d, %Y %I:%M %p")
                    status = (
                        "Overdue"
                        if schedule.status == ScheduleStatus.ACTIVE and due < now
                        else schedule.status.value.replace("_", " ").title()
                    )
                self._set_table_row(
                    self.scheduled_scripts,
                    row,
                    [
                        (
                            "Reminder"
                            if schedule.action.kind == "notify_only"
                            else "Work brief"
                            if schedule.action.kind == "command_center_brief"
                            else "Automation"
                        ),
                        schedule.name,
                        due_text,
                        status,
                        (
                            "Low"
                            if not schedule.skill_snapshot
                            else max(
                                schedule.skill_snapshot,
                                key=lambda item: {
                                    "low": 0,
                                    "medium": 1,
                                    "high": 2,
                                    "critical": 3,
                                }[item.risk.value],
                            ).risk.value.title()
                        ),
                    ],
                )
                id_item = self.scheduled_scripts.item(row, 0)
                id_item.setData(Qt.ItemDataRole.UserRole, str(schedule.schedule_id))
                reason = schedule.review_reason or schedule.paused_reason
                if reason:
                    self.scheduled_scripts.item(row, 3).setToolTip(reason)
                if selected_schedule_id == str(schedule.schedule_id):
                    self.scheduled_scripts.selectRow(row)

            self.pending_approvals.setRowCount(0)
            pending_runs = container.schedule_store.list_runs(
                states=(ScheduleRunState.AWAITING_APPROVAL,),
            )
            schedules_by_id = {
                schedule.schedule_id: schedule for schedule in container.schedule_store.list()
            }
            for row, run in enumerate(pending_runs):
                schedule = schedules_by_id.get(run.schedule_id)
                if schedule is None:
                    continue
                risk = (
                    max(
                        schedule.skill_snapshot,
                        key=lambda item: {
                            "low": 0,
                            "medium": 1,
                            "high": 2,
                            "critical": 3,
                        }[item.risk.value],
                    ).risk.value.title()
                    if schedule.skill_snapshot
                    else "Low"
                )
                expires = (
                    run.approval_expires_at.astimezone().strftime("%b %d, %I:%M:%S %p")
                    if run.approval_expires_at
                    else "Unknown"
                )
                self._set_table_row(
                    self.pending_approvals,
                    row,
                    [
                        schedule.name,
                        schedule.action.kind.replace("_", " ").title(),
                        risk,
                        expires,
                        run.blocked_reason or "Waiting for review",
                    ],
                )
                id_item = self.pending_approvals.item(row, 0)
                id_item.setData(Qt.ItemDataRole.UserRole, str(run.run_id))
                if selected_run_id == str(run.run_id):
                    self.pending_approvals.selectRow(row)

            self.schedule_history.setRowCount(0)
            for row, run in enumerate(container.schedule_store.list_runs(limit=50)):
                schedule = schedules_by_id.get(run.schedule_id)
                details = run.blocked_reason or run.result_status or ""
                self._set_table_row(
                    self.schedule_history,
                    row,
                    [
                        schedule.name if schedule else str(run.schedule_id),
                        run.scheduled_for.astimezone().strftime("%b %d, %I:%M:%S %p"),
                        run.state.value.replace("_", " ").title(),
                        str(run.attempt),
                        details,
                    ],
                )
                id_item = self.schedule_history.item(row, 0)
                id_item.setData(Qt.ItemDataRole.UserRole, str(run.run_id))

            hooks = [
                (
                    "Push to talk",
                    container.settings.push_to_talk_hotkey,
                    "Active" if self._hotkeys else "Window only",
                ),
                (
                    "Emergency stop",
                    container.settings.cancel_hotkey,
                    "Active" if self._hotkeys else "Window only",
                ),
                (
                    "Voice activation",
                    container.settings.voice_activation_mode,
                    "Active"
                    if self._passive_voice_worker and self._passive_voice_worker.isRunning()
                    else "Hotkey only",
                ),
                ("Pipeline events", "Assistant activity", "Active"),
            ]
            self.script_hooks.setRowCount(0)
            for row, hook in enumerate(hooks):
                self._set_table_row(self.script_hooks, row, list(hook))

        @staticmethod
        def _selected_table_id(table: QTableWidget) -> str | None:
            row = table.currentRow()
            if row < 0:
                return None
            item = table.item(row, 0)
            value = item.data(Qt.ItemDataRole.UserRole) if item else None
            return str(value) if value else None

        def _cancel_selected_script(self) -> None:
            row = self.running_scripts.currentRow()
            if row < 0:
                return
            item = self.running_scripts.item(row, 0)
            correlation_id = item.data(Qt.ItemDataRole.UserRole) if item else None
            if not correlation_id:
                return

            async def cancel_job():
                from uuid import UUID

                await container.engine.cancel(UUID(correlation_id))

            worker = AsyncWorker(cancel_job())
            worker.complete.connect(lambda _: self._refresh_active_scripts())
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _selected_schedule(self):
            schedule_id = self._selected_table_id(self.scheduled_scripts)
            if schedule_id is None:
                self.statusBar().showMessage("Select an automation first.", 3000)
                return None
            try:
                return container.schedule_store.get(UUID(schedule_id))
            except (KeyError, ValueError) as error:
                self._show_error(str(error))
                return None

        def _toggle_automation_pause(self) -> None:
            if container.scheduler.paused:
                container.scheduler.resume()
                self.statusBar().showMessage("Automations resumed.", 3000)
                paused = False
            else:
                container.scheduler.pause()
                self.statusBar().showMessage("All automations paused.", 3000)
                paused = True
            updated = container.settings.model_copy(update={"automations_paused": paused})
            self._persist_application_settings(updated)
            self._refresh_active_scripts()

        def _pause_or_resume_schedule(self) -> None:
            schedule = self._selected_schedule()
            if schedule is None:
                return
            try:
                if schedule.status == ScheduleStatus.PAUSED:
                    container.schedule_store.resume(schedule.schedule_id)
                else:
                    container.schedule_store.pause(schedule.schedule_id)
            except (KeyError, ValueError) as error:
                self._show_error(str(error))
            self._refresh_active_scripts()

        def _run_selected_schedule(self) -> None:
            schedule = self._selected_schedule()
            if schedule is None:
                return
            try:
                container.schedule_store.run_now(schedule.schedule_id)
                container.scheduler.wake()
            except (KeyError, ValueError) as error:
                self._show_error(str(error))
            self._refresh_active_scripts()

        def _revoke_selected_schedule(self) -> None:
            schedule = self._selected_schedule()
            if schedule is None:
                return
            answer = QMessageBox.question(
                self,
                "Revoke Background Authority",
                f"Stop {schedule.name} until its exact action is reviewed again?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                container.schedule_store.revoke_authorization(
                    schedule.schedule_id,
                    "Background authorization revoked by user",
                )
            except (KeyError, ValueError) as error:
                self._show_error(str(error))
            self._refresh_active_scripts()

        def _delete_selected_schedule(self) -> None:
            schedule = self._selected_schedule()
            if schedule is None:
                return
            answer = QMessageBox.question(
                self,
                "Delete Automation",
                f"Delete {schedule.name} and cancel its pending occurrences?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                container.schedule_store.delete(schedule.schedule_id)
            except (KeyError, ValueError) as error:
                self._show_error(str(error))
            self._refresh_active_scripts()

        def _clear_completed_schedule_history(self) -> None:
            answer = QMessageBox.question(
                self,
                "Clear Automation History",
                "Delete completed automation run records? Active and pending runs are kept.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            deleted = container.schedule_store.clear_completed_run_history()
            self.statusBar().showMessage(f"Deleted {deleted} completed run records.", 5000)
            self._refresh_active_scripts()

        def _submit_scheduler_work(self, coroutine, on_success) -> None:
            try:
                future = container.background_runtime.submit(coroutine)
            except Exception as error:
                self._show_error(str(error))
                return

            def completed(result_future) -> None:
                try:
                    payload = (on_success, result_future.result(), None)
                except Exception as error:
                    payload = (on_success, None, error)
                self.hotkey_bridge.automation_complete.emit(payload)

            future.add_done_callback(completed)

        def _handle_automation_completion(self, payload: tuple) -> None:
            callback, result, error = payload
            if error is not None:
                self._show_error(str(error))
            else:
                callback(result)
            self._refresh_active_scripts()

        def _review_selected_approval(self) -> None:
            run_id = self._selected_table_id(self.pending_approvals)
            if run_id is None:
                self.statusBar().showMessage("Select a pending approval first.", 3000)
                return
            self._submit_scheduler_work(
                container.scheduler.runner.prepare_approval(UUID(run_id)),
                self._confirm_approval_review,
            )

        def _confirm_approval_review(self, review) -> None:
            lines = [
                f"Schedule: {review.schedule_name}",
                f"Risk: {review.risk.value.title()}",
                "",
            ]
            for item in review.confirmations:
                request = item.request
                lines.append(f"{item.step_id}: {request.exact_action}")
                if request.exact_targets:
                    lines.append("Targets: " + "; ".join(request.exact_targets))
            answer = QMessageBox.question(
                self,
                "Approve Scheduled Occurrence",
                "\n".join(lines),
            )
            if answer != QMessageBox.StandardButton.Yes:
                for item in review.confirmations:
                    container.confirmations.deny(item.request.confirmation_id)
                return
            confirmation_ids = {}
            typed_tokens = {}
            for item in review.confirmations:
                request = item.request
                confirmation_ids[item.step_id] = request.confirmation_id
                if request.requires_typed_token:
                    token, ok = QInputDialog.getText(
                        self,
                        "Critical Action Confirmation",
                        "Type the final six characters of the confirmation ID "
                        f"{request.confirmation_id}:",
                    )
                    if not ok:
                        container.confirmations.deny(request.confirmation_id)
                        return
                    typed_tokens[item.step_id] = token.strip()
            self._submit_scheduler_work(
                container.scheduler.runner.approve(
                    review.run_id,
                    confirmation_ids,
                    typed_tokens=typed_tokens,
                ),
                lambda run: self.statusBar().showMessage(
                    f"Scheduled occurrence finished: {run.state.value}.",
                    5000,
                ),
            )

        def _reject_selected_approval(self) -> None:
            run_id = self._selected_table_id(self.pending_approvals)
            if run_id is None:
                self.statusBar().showMessage("Select a pending approval first.", 3000)
                return
            try:
                container.scheduler.runner.reject(UUID(run_id))
            except (KeyError, ValueError) as error:
                self._show_error(str(error))
            self._refresh_active_scripts()

        def _command_tab(self) -> QWidget:
            page = QWidget()
            page.setObjectName("assistantPage")
            layout = QVBoxLayout(page)
            layout.setContentsMargins(32, 28, 32, 28)
            layout.setSpacing(18)

            heading_row = QHBoxLayout()
            eyebrow = QLabel("PRIVATE  |  LOCAL  |  READY")
            eyebrow.setObjectName("eyebrow")
            self.notification_button = QPushButton("Inbox")
            self.notification_button.setObjectName("secondaryButton")
            self.notification_button.setAccessibleName("Open notification drawer")
            self.notification_button.clicked.connect(self._toggle_notification_drawer)
            self.compact_view_button = QPushButton("Compact view")
            self.compact_view_button.setObjectName("secondaryButton")
            self.compact_view_button.setAccessibleName("Open compact assistant")
            self.compact_view_button.clicked.connect(lambda: self._switch_window_mode("compact"))
            heading_row.addWidget(eyebrow, 1)
            heading_row.addWidget(self.notification_button)
            heading_row.addWidget(self.compact_view_button)
            title = QLabel("What can I help with?")
            title.setObjectName("pageTitle")
            subtitle = QLabel(
                "Type a request or use your microphone. Actions and approvals "
                "will appear here as they happen."
            )
            subtitle.setObjectName("pageSubtitle")
            subtitle.setWordWrap(True)
            layout.addLayout(heading_row)
            layout.addWidget(title)
            layout.addWidget(subtitle)

            status_card = QFrame()
            status_card.setObjectName("statusCard")
            status_layout = QVBoxLayout(status_card)
            status_layout.setContentsMargins(20, 16, 20, 16)
            status_layout.setSpacing(8)
            status_row = QHBoxLayout()
            self.status_dot = QLabel("")
            self.status_dot.setObjectName("statusDot")
            self.status_dot.setFixedSize(10, 10)
            self.status = QLabel("Ready")
            self.status.setObjectName("assistantStatus")
            self.status.setAccessibleName("Assistant status")
            self.status_detail = QLabel("Waiting for your request")
            self.status_detail.setObjectName("statusDetail")
            self.status_transcript = QLabel("")
            self.status_transcript.setObjectName("statusTranscript")
            self.status_transcript.setAccessibleName("Live speech transcript")
            self.status_transcript.setWordWrap(True)
            self.elapsed_status = QLabel("")
            self.elapsed_status.setObjectName("elapsedStatus")
            status_row.addWidget(self.status_dot)
            status_row.addWidget(self.status, 1)
            status_row.addWidget(self.elapsed_status)
            self.pipeline_progress = QProgressBar()
            self.pipeline_progress.setObjectName("pipelineProgress")
            self.pipeline_progress.setTextVisible(False)
            self.pipeline_progress.setFixedHeight(4)
            self.pipeline_progress.hide()
            mic_row = QHBoxLayout()
            mic_label = QLabel("MIC")
            mic_label.setObjectName("sectionLabel")
            self.mic_level = QProgressBar()
            self.mic_level.setObjectName("micLevel")
            self.mic_level.setAccessibleName("Microphone input level")
            self.mic_level.setTextVisible(False)
            self.mic_level.setRange(0, 100)
            self.mic_level.setValue(0)
            self.mic_level.setFixedHeight(8)
            self.mic_level_value = QLabel("0%")
            self.mic_level_value.setObjectName("micLevelValue")
            self.mic_level_value.setAccessibleName("Microphone input level percentage")
            self.mic_level_value.setMinimumWidth(36)
            mic_row.addWidget(mic_label)
            mic_row.addWidget(self.mic_level, 1)
            mic_row.addWidget(self.mic_level_value)
            status_layout.addLayout(status_row)
            status_layout.addWidget(self.status_detail)
            status_layout.addWidget(self.status_transcript)
            status_layout.addWidget(self.pipeline_progress)
            status_layout.addLayout(mic_row)
            layout.addWidget(status_card)

            content = QHBoxLayout()
            content.setSpacing(18)
            compose_card = QFrame()
            compose_card.setObjectName("composeCard")
            compose_layout = QVBoxLayout(compose_card)
            compose_layout.setContentsMargins(20, 18, 20, 18)
            compose_layout.setSpacing(12)
            input_label = QLabel("YOUR REQUEST")
            input_label.setObjectName("sectionLabel")
            self.transcript = RequestTextEdit()
            self.transcript.setObjectName("commandInput")
            self.transcript.setPlaceholderText(
                "Ask a question, open an app, manage a file, or run a skill..."
            )
            self.transcript.setAccessibleName("Command input and transcript")
            self.transcript.setMinimumHeight(120)
            self.transcript.submitted.connect(self.run_command)
            controls = QHBoxLayout()
            controls.setSpacing(10)
            self.listen_button = QPushButton("  Listen")
            self.listen_button.setObjectName("secondaryButton")
            self.listen_button.setAccessibleName("Start voice input")
            self.listen_button.clicked.connect(self.listen)
            self.run_button = QPushButton("Run request  >")
            self.run_button.setObjectName("primaryButton")
            self.run_button.setAccessibleName("Run typed request")
            self.run_button.clicked.connect(self.run_command)
            self.cancel_button = QPushButton("Stop")
            self.cancel_button.setObjectName("dangerButton")
            self.cancel_button.setAccessibleName("Stop current request")
            self.cancel_button.clicked.connect(self.cancel)
            self.cancel_button.setEnabled(False)
            self.stop_speaking_button = QPushButton("Stop speaking")
            self.stop_speaking_button.setObjectName("secondaryButton")
            self.stop_speaking_button.setAccessibleName("Stop assistant speech")
            self.stop_speaking_button.clicked.connect(self.stop_speaking)
            self.stop_speaking_button.setEnabled(False)
            controls.addWidget(self.listen_button)
            controls.addStretch()
            controls.addWidget(self.stop_speaking_button)
            controls.addWidget(self.cancel_button)
            controls.addWidget(self.run_button)
            response_header = QHBoxLayout()
            response_label = QLabel("ASSISTANT")
            response_label.setObjectName("sectionLabel")
            self.response = QTextEdit()
            self.response.setObjectName("assistantResponse")
            self.response.setReadOnly(True)
            self.response.setPlaceholderText("The response will appear here.")
            self.response.setAccessibleName("Assistant response")
            self.response.setMinimumHeight(160)
            self.media_viewer = MediaViewer(
                controller.media_session_registry,
                audio_focus=container.audio_focus,
            )
            self.result_window = ResultWindow(self.media_viewer)
            self.result_window.automation_requested.connect(
                lambda run_id: self._show_automation_run(UUID(run_id))
            )
            self.result_window.setGeometry(
                self._bounded_window_geometry(
                    self._desktop_state.result_geometry,
                    default_width=920,
                    default_height=680,
                    minimum_width=640,
                    minimum_height=440,
                )
            )
            self.full_media_card = MediaCompactCard()
            self.full_media_card.open_requested.connect(self._open_media_viewer)
            self.full_media_card.close_requested.connect(self.media_viewer.clear)
            self.full_media_card.play_pause_requested.connect(self.media_viewer.play_pause)
            self.media_viewer.session_changed.connect(self.full_media_card.set_snapshot)
            self.response_type = QLabel("READY")
            self.response_type.setObjectName("responseBadge")
            self.response_type.setAccessibleName("Assistant response type")
            self.inspect_sources_button = QPushButton("Inspect sources")
            self.inspect_sources_button.setObjectName("secondaryButton")
            self.inspect_sources_button.setAccessibleName("Inspect response sources")
            self.inspect_sources_button.setEnabled(False)
            self.inspect_sources_button.clicked.connect(self._inspect_sources)
            self.copy_sources_button = QPushButton("Copy sources")
            self.copy_sources_button.setObjectName("secondaryButton")
            self.copy_sources_button.setAccessibleName("Copy response sources")
            self.copy_sources_button.setEnabled(False)
            self.copy_sources_button.clicked.connect(self._copy_sources)
            self._last_sources_text = ""
            response_header.addWidget(response_label)
            response_header.addStretch()
            response_header.addWidget(self.inspect_sources_button)
            response_header.addWidget(self.copy_sources_button)
            response_header.addWidget(self.response_type)
            compose_layout.addLayout(response_header)
            compose_layout.addWidget(self.response)
            compose_layout.addWidget(self.full_media_card)
            compose_layout.addSpacing(4)
            compose_layout.addWidget(input_label)
            compose_layout.addWidget(self.transcript)
            compose_layout.addLayout(controls)

            activity_card = QFrame()
            activity_card.setObjectName("activityCard")
            activity_card.setMinimumWidth(280)
            activity_card.setMaximumWidth(390)
            activity_layout = QVBoxLayout(activity_card)
            activity_layout.setContentsMargins(18, 18, 18, 18)
            activity_layout.setSpacing(10)
            activity_title = QLabel("TOOLS + ACTIONS")
            activity_title.setObjectName("sectionLabel")
            self.activity_hint = QLabel("Tool names, safe inputs, live actions, and outcomes")
            self.activity_hint.setObjectName("activityHint")
            self.activity_hint.setWordWrap(True)
            self.background_activity = QLabel("BACKGROUND  •  IDLE")
            self.background_activity.setObjectName("activityHint")
            self.activity_log = QListWidget()
            self.activity_log.setObjectName("activityLog")
            self.activity_log.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.activity_log.setWordWrap(True)
            self.activity_log.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.activity_log.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            activity_layout.addWidget(activity_title)
            activity_layout.addWidget(self.activity_hint)
            activity_layout.addWidget(self.background_activity)
            activity_layout.addWidget(self.activity_log)
            self._append_activity("Ready", "Waiting for your request")

            content.addWidget(compose_card, 3)
            content.addWidget(activity_card, 1)
            layout.addLayout(content, 1)
            return page

        def _append_activity(self, label: str, detail: str = "") -> None:
            timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
            text = f"{timestamp}   {label}"
            if detail and detail.lower() != label.lower():
                text += f"\n          {detail}"
            self.activity_log.addItem(text)
            while self.activity_log.count() > 200:
                self.activity_log.takeItem(0)
            self.activity_log.scrollToBottom()

        def _append_trace_activity(self, trace: Any) -> None:
            prefix = "Background · " if trace.invocation == "background" else ""
            label = prefix + trace.label
            if trace.tool_name:
                label += f" · {trace.tool_name}"
            lines = []
            if trace.skill_id:
                lines.append(f"TOOL    {trace.skill_id}")
            if trace.action:
                lines.append(f"ACTION  {trace.action}")
            if trace.detail and trace.detail.casefold() != (trace.action or "").casefold():
                lines.append(trace.detail)
            if trace.status is not None:
                status = trace.status.value.replace("_", " ").upper()
                duration = (
                    f"  ·  {trace.duration_ms / 1000:.2f}s" if trace.duration_ms is not None else ""
                )
                lines.append(f"STATUS  {status}{duration}")
            self._append_activity(label, "\n".join(lines))

        def _update_background_activity(self) -> None:
            count = sum(self._background_correlations.values())
            self.background_activity.setText(
                "BACKGROUND  •  IDLE" if count == 0 else f"BACKGROUND  •  {count} ACTIVE"
            )

        def _set_pipeline_state(
            self,
            state: PipelineState,
            message: str = "",
            progress: float | None = None,
            *,
            record_activity: bool = True,
        ) -> None:
            controller.set_dashboard_pipeline_state(state)
            labels = {
                PipelineState.IDLE: "Ready",
                PipelineState.LISTENING: "Listening",
                PipelineState.TRANSCRIBING: "Transcribing",
                PipelineState.THINKING: "Thinking",
                PipelineState.AWAITING_APPROVAL: "Waiting for approval",
                PipelineState.EXECUTING: "Executing action",
                PipelineState.SPEAKING: "Speaking",
                PipelineState.ERROR: "Something went wrong",
                PipelineState.CANCELLED: "Cancelled",
            }
            was_active = self._pipeline_is_busy()
            active = state in self.ACTIVE_PIPELINE_STATES
            self._active_state = state
            if active:
                self.media_viewer.suspend_for_assistant()
            elif state in {
                PipelineState.IDLE,
                PipelineState.ERROR,
            }:
                self.media_viewer.resume_after_assistant()
            self._status_frame = 0
            self.status.setText(labels[state])
            detail = message or {
                PipelineState.IDLE: "Waiting for your request",
                PipelineState.THINKING: "Understanding what you asked",
                PipelineState.EXECUTING: "A local skill is running",
            }.get(state, labels[state])
            self.status_detail.setText(detail)
            self.compact_panel.status.setText(labels[state])
            self.compact_panel.status_detail.setText(detail)
            self.status_dot.setProperty("state", state.value)
            self.status_dot.style().unpolish(self.status_dot)
            self.status_dot.style().polish(self.status_dot)
            self.compact_panel.status_dot.setProperty("state", state.value)
            self.compact_panel.status_dot.style().unpolish(self.compact_panel.status_dot)
            self.compact_panel.status_dot.style().polish(self.compact_panel.status_dot)
            self._update_command_controls()
            if state in {PipelineState.LISTENING, PipelineState.TRANSCRIBING}:
                self._mic_level_timer.start()
            else:
                self._mic_level_timer.stop()
                self._set_mic_level_value(0)
            if active:
                if not was_active:
                    self._activity_started = time.perf_counter()
                elapsed = time.perf_counter() - self._activity_started
                self.elapsed_status.setText(f"{elapsed:.1f}s")
                self.pipeline_progress.show()
                self.compact_panel.pipeline_progress.show()
                if progress is None:
                    self.pipeline_progress.setRange(0, 0)
                    self.compact_panel.pipeline_progress.setRange(0, 0)
                else:
                    self.pipeline_progress.setRange(0, 100)
                    self.pipeline_progress.setValue(round(progress * 100))
                    self.compact_panel.pipeline_progress.setRange(0, 100)
                    self.compact_panel.pipeline_progress.setValue(round(progress * 100))
                if container.settings.ui_reduced_motion:
                    self.pipeline_progress.setRange(0, 100)
                    self.pipeline_progress.setValue(round((progress or 0.0) * 100))
                    self.compact_panel.pipeline_progress.setRange(0, 100)
                    self.compact_panel.pipeline_progress.setValue(round((progress or 0.0) * 100))
                    self._status_timer.stop()
                else:
                    self._status_timer.start()
            else:
                self._status_timer.stop()
                self.elapsed_status.setText("")
                self.pipeline_progress.hide()
                self.compact_panel.pipeline_progress.hide()
            if record_activity:
                self._append_activity(labels[state], message)

        def _pipeline_is_busy(self) -> bool:
            return self._active_state in self.ACTIVE_PIPELINE_STATES

        def _update_command_controls(self) -> None:
            active = self._pipeline_is_busy()
            blocked = active or self._voice_prompt_active
            self.run_button.setEnabled(not blocked)
            self.listen_button.setEnabled(not blocked)
            self.cancel_button.setEnabled(active)
            speech_active = (
                self._active_state == PipelineState.SPEAKING
                or container.orchestrator.speech_in_progress
            )
            self.stop_speaking_button.setEnabled(speech_active)
            self.compact_view_button.setEnabled(not blocked)
            self.compact_panel.set_pipeline_busy(
                blocked=blocked,
                cancellable=active,
                speech_cancellable=speech_active,
            )

        def _add_shortcut(self, sequence: str, callback: Any) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

        def _animate_status(self) -> None:
            self._status_frame = (self._status_frame + 1) % 4
            base = {
                PipelineState.LISTENING: "Listening",
                PipelineState.TRANSCRIBING: "Transcribing",
                PipelineState.THINKING: "Thinking",
                PipelineState.EXECUTING: "Executing action",
                PipelineState.SPEAKING: "Speaking",
            }.get(self._active_state)
            if base:
                animated = base + "." * self._status_frame
                self.status.setText(animated)
                self.compact_panel.status.setText(animated)
                elapsed = time.perf_counter() - self._activity_started
                self.elapsed_status.setText(f"{elapsed:.1f}s")

        def _update_mic_level(self) -> None:
            level = min(1.0, max(0.0, container.audio_capture.level))
            threshold = max(container.audio_capture.vad_threshold, 0.001)
            self._set_mic_level_value(round(min(1.0, level / (threshold * 4)) * 100))

        def _set_mic_level_value(self, value: int) -> None:
            value = max(0, min(100, value))
            self.mic_level.setValue(value)
            self.mic_level_value.setText(f"{value}%")
            self.compact_panel.mic_level.setValue(value)
            self.compact_panel.mic_level_value.setText(f"{value}%")

        def _register_global_hotkeys(self) -> None:
            try:
                import keyboard

                self._hotkeys = [
                    keyboard.add_hotkey(
                        container.settings.push_to_talk_hotkey,
                        self.hotkey_bridge.push_to_talk.emit,
                        trigger_on_release=True,
                    ),
                    keyboard.add_hotkey(
                        container.settings.cancel_hotkey,
                        self.hotkey_bridge.cancel.emit,
                    ),
                ]
            except Exception:
                self.statusBar().showMessage(
                    "Global hotkeys unavailable; window shortcuts remain active."
                )

        def _start_wakeword(self) -> None:
            async def monitor():
                async for _score in container.wakeword.detections():
                    self.hotkey_bridge.push_to_talk.emit()

            worker = AsyncWorker(monitor())
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _start_passive_voice(self) -> None:
            if self._voice_prompt_active:
                return
            self._stop_passive_voice()
            if container.settings.voice_activation_mode == "hotkey":
                return
            self._passive_voice_restart_enabled = True
            worker = PassiveVoiceWorker(
                container,
                container.settings.voice_activation_mode,
            )
            worker.complete.connect(self._show_voice_result)
            worker.failed.connect(self._show_error)
            worker.partial_transcript.connect(self._show_voice_transcript)
            worker.request_transcript.connect(self._commit_voice_transcript)
            worker.finished.connect(
                lambda worker=worker: self._handle_passive_voice_finished(worker)
            )
            self._passive_voice_worker = worker
            worker.start()
            self._apply_passive_voice_indicators()

        def _stop_passive_voice(self) -> None:
            self._passive_voice_restart_enabled = False
            worker = self._passive_voice_worker
            if worker is None:
                return
            worker.stop()
            asyncio.run(container.audio_capture.stop())
            worker.wait(1500)
            if self._passive_voice_worker is worker:
                self._passive_voice_worker = None
            self._mic_level_timer.stop()
            self._set_mic_level_value(0)

        def _handle_passive_voice_finished(self, worker: PassiveVoiceWorker) -> None:
            if self._passive_voice_worker is not worker:
                return
            self._passive_voice_worker = None
            if (
                self._passive_voice_restart_enabled
                and container.settings.voice_activation_mode != "hotkey"
            ):
                QTimer.singleShot(250, self._start_passive_voice)

        def _apply_passive_voice_indicators(self) -> None:
            if self._passive_voice_worker and self._passive_voice_worker.isRunning():
                self.status.setText("Listening")
                self.compact_panel.status.setText("Listening")
                self.status_dot.setProperty("state", PipelineState.LISTENING.value)
                self.status_dot.style().unpolish(self.status_dot)
                self.status_dot.style().polish(self.status_dot)
                self.compact_panel.status_dot.setProperty("state", PipelineState.LISTENING.value)
                self.compact_panel.status_dot.style().unpolish(self.compact_panel.status_dot)
                self.compact_panel.status_dot.style().polish(self.compact_panel.status_dot)
                detail = self._passive_voice_status()
                self.status_detail.setText(detail)
                self.compact_panel.status_detail.setText(detail)
                self.listen_button.setEnabled(False)
                self.compact_panel.listen_button.setEnabled(False)
                self._mic_level_timer.start()

        def _passive_voice_status(self) -> str:
            if container.settings.voice_activation_mode == "persistent":
                return "Persistent voice mode is listening"
            if container.settings.voice_activation_mode == "keyword_clap":
                triggers = []
                if container.settings.voice_activation_keyword_enabled:
                    triggers.append(f'"{container.settings.voice_activation_keyword}"')
                if container.settings.voice_activation_clap_enabled:
                    triggers.append("a clap")
                return "Listening for " + " or ".join(triggers)
            return "Waiting for your request"

        def listen(self) -> None:
            if self._pipeline_is_busy():
                self.statusBar().showMessage("Voice input is already active.", 1500)
                return
            self._pending_voice_transcript = None
            self._set_pipeline_state(PipelineState.LISTENING, "Microphone is active")
            worker = AsyncWorker(
                container.orchestrator.listen_once(container.stt, container.audio_capture.chunks())
            )
            worker.complete.connect(self._show_voice_result)
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _handle_pipeline_event(self, event: PipelineEvent) -> None:
            if response_data := event.data.get("response"):
                try:
                    ready_response = Response.model_validate(response_data)
                except (TypeError, ValueError):
                    ready_response = None
                if ready_response is not None:
                    self._display_response(ready_response)
                    self._last_event_response = ready_response
            if event.trace is not None:
                self._append_trace_activity(event.trace)
                if event.trace.invocation == "background":
                    if event.trace.phase.value == "action":
                        self._background_correlations[event.correlation_id] = (
                            self._background_correlations.get(event.correlation_id, 0) + 1
                        )
                    elif event.trace.phase.value == "result":
                        remaining = self._background_correlations.get(event.correlation_id, 0) - 1
                        if remaining > 0:
                            self._background_correlations[event.correlation_id] = remaining
                        else:
                            self._background_correlations.pop(event.correlation_id, None)
                    self._update_background_activity()
            if not event.updates_foreground_status:
                return
            self._set_pipeline_state(
                event.state,
                event.message,
                event.progress,
                record_activity=event.trace is None,
            )
            if event.state == PipelineState.TRANSCRIBING and event.message:
                self._show_voice_transcript(event.message)

        def _handle_background_notification(self, message: Any) -> None:
            tray = getattr(self, "tray", None)
            foreground = self.isVisible() and self.isActiveWindow()
            quiet = _notification_quiet_hours_active(container.settings)
            may_interrupt = not quiet or message.severity == "error"
            if foreground and container.settings.show_in_app_notifications and may_interrupt:
                self.notification_banner.show_notification(message)
                self._position_notification_overlays()
            elif (
                container.settings.show_system_notifications
                and may_interrupt
                and tray is not None
                and QSystemTrayIcon.supportsMessages()
            ):
                icons = {
                    "info": QSystemTrayIcon.MessageIcon.Information,
                    "success": QSystemTrayIcon.MessageIcon.Information,
                    "warning": QSystemTrayIcon.MessageIcon.Warning,
                    "error": QSystemTrayIcon.MessageIcon.Critical,
                }
                tray.showMessage(
                    message.title,
                    message.message,
                    icons.get(message.severity, QSystemTrayIcon.MessageIcon.Information),
                    10_000,
                )
                self._last_tray_notification_id = str(message.notification_id)
            self.statusBar().showMessage(message.message, 10_000)
            self._refresh_notification_ui()
            self._refresh_active_scripts()

        def _show_voice_transcript(self, text: str) -> None:
            self._pending_voice_transcript = text
            self._set_status_transcript(text)

        def _commit_voice_transcript(self, text: str) -> None:
            text = text.strip()
            if not text:
                return
            self.transcript.setPlainText(text)
            self.transcript.moveCursor(QTextCursor.MoveOperation.End)
            self.compact_panel.set_request(text)
            self._set_status_transcript(text)
            self._pending_voice_transcript = None

        def _set_status_transcript(self, text: str) -> None:
            text = " ".join(text.split())
            display = f"Heard: {text}" if text else ""
            self.status_transcript.setText(display)
            self.compact_panel.status_transcript.setText(display)

        def _voice_prompt_choice(
            self,
            *,
            title: str,
            text: str,
            informative_text: str,
            kind: str,
            buttons: list[tuple[str, str, Any]],
            default_choice: str,
            expected_token: str | None = None,
        ) -> str:
            restart_passive = container.settings.voice_activation_mode != "hotkey"
            self._voice_prompt_active = True
            self._stop_passive_voice()
            self._update_command_controls()
            self.show_from_tray()
            choice = {"value": default_choice}
            button_choices: dict[Any, str] = {}
            dialog = QMessageBox(self)
            dialog.setWindowTitle(title)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setText(text)
            dialog.setInformativeText(informative_text)
            for value, label, role in buttons:
                button_choices[dialog.addButton(label, role)] = value

            worker = VoicePromptWorker(container)

            def accept_spoken(transcript: str) -> None:
                self._set_status_transcript(transcript)
                response = parse_voice_prompt_response(
                    transcript,
                    kind,
                    expected_token=expected_token,
                )
                if response is None:
                    self.statusBar().showMessage("I did not catch a valid prompt response.", 1800)
                    return
                if response.action == "approve":
                    choice["value"] = "approve"
                elif response.action == "deny":
                    choice["value"] = "deny"
                elif response.action == "once":
                    choice["value"] = "once"
                elif response.action == "session":
                    choice["value"] = "session"
                elif response.action == "always":
                    choice["value"] = "always"
                elif response.action == "requested_targets":
                    choice["value"] = "requested_targets"
                elif response.action == "any_target":
                    choice["value"] = "any_target"
                elif response.action == "token":
                    choice["value"] = response.value or default_choice
                dialog.done(1)

            worker.complete.connect(accept_spoken)
            worker.failed.connect(
                lambda error: self.statusBar().showMessage(
                    f"Voice prompt unavailable: {error}", 2500
                )
            )
            worker.partial_transcript.connect(self._show_voice_transcript)
            worker.finished.connect(lambda: self._discard_worker(worker))
            self._workers.append(worker)
            worker.start()
            self._set_pipeline_state(
                PipelineState.AWAITING_APPROVAL,
                "Waiting for your approval by voice or button",
            )
            dialog.exec()
            clicked = dialog.clickedButton()
            if clicked in button_choices:
                choice["value"] = button_choices[clicked]
            worker.stop()
            asyncio.run(container.audio_capture.stop())
            worker.wait(1500)
            self._discard_worker(worker)
            self._voice_prompt_active = False
            self._update_command_controls()
            if restart_passive:
                QTimer.singleShot(250, self._start_passive_voice)
            return choice["value"]

        def _discard_worker(self, worker: Any) -> None:
            if worker in self._workers:
                self._workers.remove(worker)

        def _discard_dashboard_worker(self, worker: AsyncWorker) -> None:
            if worker in self._dashboard_workers:
                self._dashboard_workers.remove(worker)
            self._discard_worker(worker)

        def _extension_hub_tab(self) -> QWidget:
            from assistant.ui.desktop.extension_hub import ExtensionHubPage

            self.extension_hub_page = ExtensionHubPage(
                container.extension_hub,
                offline=self._startup_offline_lock,
            )
            self.extension_hub_page.installed.connect(self._extension_installed)
            self.extension_hub_page.uninstalled.connect(self._extension_uninstalled)
            self.extension_hub_page.connection_changed.connect(
                lambda _extension_id: self.connections_page.reload()
            )
            return self.extension_hub_page

        def _connections_tab(self) -> QWidget:
            from assistant.ui.desktop.connections import ConnectionsPage

            self.connections_page = ConnectionsPage(controller)
            self.connections_page.extension_hub_requested.connect(
                self._open_extension_hub_from_connections
            )
            self.connections_page.automation_requested.connect(
                self._open_automation_from_connections
            )
            self.connections_page.work_brief_requested.connect(self._build_and_save_work_brief)
            self.connections_page.delivery_prepare_requested.connect(
                self._prepare_command_center_delivery
            )
            return self.connections_page

        def _create_work_brief_automation(self) -> None:
            self.connections_page.create_work_brief_automation()

        def _open_automation_from_connections(self, schedule_id: UUID | None) -> None:
            self.tabs.setCurrentIndex(TAB_LABELS.index("Automations"))
            self._refresh_active_scripts()
            if schedule_id is None:
                return
            for row in range(self.scheduled_scripts.rowCount()):
                item = self.scheduled_scripts.item(row, 0)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == str(schedule_id):
                    self.scheduled_scripts.selectRow(row)
                    self.scheduled_scripts.scrollToItem(item)
                    break
            self.statusBar().showMessage(
                "Work brief automation created and selected.",
                8000,
            )

        def _build_and_save_work_brief(self) -> None:
            self._show_primary_screen("assistant")
            self._run_text_command("brief me and save it")

        def _prepare_command_center_delivery(self, delivery_id: UUID) -> None:
            worker = AsyncWorker(controller.prepare_command_center_delivery(delivery_id))
            worker.complete.connect(
                lambda payload: self._show_command_center_delivery_outcome(
                    delivery_id,
                    payload[1],
                    recorded_pending=payload[0],
                )
            )
            worker.failed.connect(
                lambda message: QMessageBox.warning(
                    self,
                    "Delivery could not continue",
                    message,
                )
            )
            worker.finished.connect(lambda: self._discard_worker(worker))
            self._workers.append(worker)
            worker.start()

        def _show_command_center_delivery_outcome(
            self,
            delivery_id: UUID,
            outcome: Any,
            *,
            recorded_pending: Any | None = None,
        ) -> None:
            pending = recorded_pending or controller.finalize_command_center_delivery(
                delivery_id,
                outcome,
            )
            self.connections_page.reload()
            if outcome.registration_requests:
                self._request_registrations(
                    outcome,
                    continuation=lambda continued: self._show_command_center_delivery_outcome(
                        delivery_id,
                        continued,
                    ),
                    cancelled=lambda: controller.cancel_command_center_delivery(delivery_id),
                )
            elif outcome.permission_requests:
                self._request_permissions(
                    outcome,
                    continuation=lambda payload: self._show_command_center_delivery_outcome(
                        delivery_id,
                        payload[1],
                        recorded_pending=payload[0],
                    ),
                    cancelled=lambda: controller.cancel_command_center_delivery(delivery_id),
                    resume=lambda: controller.prepare_command_center_delivery(delivery_id),
                )
            elif outcome.confirmation:
                self._confirm_command_center_delivery(delivery_id, outcome)
            elif outcome.result:
                label = pending.state.value.replace("_", " ")
                self.statusBar().showMessage(f"Reviewed delivery {label}.", 8000)

        def _finish_command_center_delivery(
            self,
            delivery_id: UUID,
            result: Any,
            *,
            recorded_pending: Any | None = None,
        ) -> None:
            if recorded_pending is None:
                controller.finalize_command_center_delivery(
                    delivery_id,
                    ExecutionOutcome(result=result),
                )
            self.connections_page.reload()
            self._show_skill_result(result)

        def _confirm_command_center_delivery(self, delivery_id: UUID, outcome: Any) -> None:
            request = outcome.confirmation
            decision = self._voice_prompt_choice(
                title="Confirm Reviewed Delivery",
                text=request.exact_action,
                informative_text="The artifact revision and exact target were revalidated.",
                kind="approval",
                buttons=[
                    ("approve", "Deliver", QMessageBox.ButtonRole.AcceptRole),
                    ("deny", "Cancel", QMessageBox.ButtonRole.RejectRole),
                ],
                default_choice="deny",
            )
            if decision != "approve":
                controller.cancel_command_center_delivery(
                    delivery_id,
                    confirmation_id=request.confirmation_id,
                )
                self.connections_page.reload()
                return
            typed_token = None
            if request.requires_typed_token:
                expected = str(request.confirmation_id)[-6:]
                typed_token, accepted = QInputDialog.getText(
                    self,
                    "Critical Delivery Confirmation",
                    f"Type {expected} to continue.",
                )
                if not accepted or typed_token != expected:
                    controller.cancel_command_center_delivery(
                        delivery_id,
                        confirmation_id=request.confirmation_id,
                    )
                    self.connections_page.reload()
                    return
            worker = AsyncWorker(
                controller.confirm_command_center_delivery(
                    delivery_id,
                    request.confirmation_id,
                    typed_token=typed_token,
                )
            )
            worker.complete.connect(
                lambda payload: self._finish_command_center_delivery(
                    delivery_id,
                    payload[1],
                    recorded_pending=payload[0],
                )
            )
            worker.failed.connect(
                lambda message: QMessageBox.warning(self, "Delivery failed", message)
            )
            worker.finished.connect(lambda: self._discard_worker(worker))
            self._workers.append(worker)
            worker.start()

        def _open_extension_hub_from_connections(self, extension_id: str) -> None:
            self.tabs.setCurrentIndex(TAB_LABELS.index("Extension Hub"))
            self.extension_hub_page.focus_extension(extension_id)
            label = (
                extension_id.replace("integration.", "").replace("_", " ").replace("-", " ").title()
            )
            self.statusBar().showMessage(
                (
                    f"Manage {label} in Extension Hub."
                    if label
                    else "Install extensions and manage their accounts here."
                ),
                8000,
            )

        def _extension_installed(self, skill_id: str) -> None:
            self._reload_skills()
            self.connections_page.reload()
            self.statusBar().showMessage(
                f"Installed {skill_id} disabled. Review it in Skills before enabling.",
                8000,
            )

        def _extension_uninstalled(self, skill_id: str) -> None:
            self._reload_skills()
            self.connections_page.reload()
            self.statusBar().showMessage(f"Uninstalled {skill_id}.", 8000)

        def _skills_tab(self) -> QWidget:
            page = QWidget()
            page.setObjectName("assistantPage")
            layout = QHBoxLayout(page)
            layout.setContentsMargins(20, 20, 20, 20)
            layout.setSpacing(18)

            browser = QFrame()
            browser.setObjectName("skillBrowserCard")
            browser.setMinimumWidth(250)
            browser.setMaximumWidth(340)
            browser_layout = QVBoxLayout(browser)
            browser_layout.setContentsMargins(16, 16, 16, 16)
            browser_layout.setSpacing(10)
            browser_title = QLabel("SKILL LIBRARY")
            browser_title.setObjectName("sectionLabel")
            browser_layout.addWidget(browser_title)
            browser_layout.addWidget(
                self._info_label(
                    "Select a skill to review its purpose, access, security level, and demo. "
                    "Skills are grouped by category; Extension Hub skills carry a source badge."
                )
            )
            self.skill_list = QListWidget()
            self.skill_list.setObjectName("skillList")
            self.skill_list.setAccessibleName("Available skills")
            self.skill_list.setWordWrap(True)
            self.skill_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.skill_list.currentItemChanged.connect(lambda *_: self._update_skill_details())
            browser_layout.addWidget(self.skill_list, 1)

            toggle = QPushButton("Enable / Disable Selected")
            toggle.setObjectName("secondaryButton")
            toggle.clicked.connect(self._toggle_skill)
            install = QPushButton("Install Signed Skill")
            install.clicked.connect(self._install_skill)
            update = QPushButton("Update Selected Skill")
            update.clicked.connect(self._update_skill)
            health = QPushButton("Check Selected Health")
            health.clicked.connect(self._check_skill_health)
            uninstall = QPushButton("Uninstall Selected Skill")
            uninstall.setObjectName("dangerButton")
            uninstall.clicked.connect(self._uninstall_skill)
            browser_layout.addWidget(toggle)
            browser_layout.addWidget(health)
            browser_layout.addWidget(install)
            browser_layout.addWidget(update)
            browser_layout.addWidget(uninstall)

            detail_scroll = QScrollArea()
            detail_scroll.setObjectName("skillDetailScroll")
            detail_scroll.setWidgetResizable(True)
            detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
            detail = QWidget()
            detail.setObjectName("skillDetailPage")
            detail_layout = QVBoxLayout(detail)
            detail_layout.setContentsMargins(4, 2, 10, 16)
            detail_layout.setSpacing(12)

            self.skill_detail_title = QLabel("Select a skill")
            self.skill_detail_title.setObjectName("skillDetailTitle")
            self.skill_detail_title.setWordWrap(True)
            self.skill_detail_id = QLabel("")
            self.skill_detail_id.setObjectName("skillDetailId")
            detail_layout.addWidget(self.skill_detail_title)
            detail_layout.addWidget(self.skill_detail_id)

            overview = QFrame()
            overview.setObjectName("skillDetailCard")
            overview_layout = QVBoxLayout(overview)
            overview_layout.setContentsMargins(18, 16, 18, 16)
            overview_header = QHBoxLayout()
            self.skill_category_value = QLabel("General")
            self.skill_category_value.setObjectName("skillCategoryBadge")
            self.skill_source_value = QLabel("Built-in")
            self.skill_source_value.setObjectName("skillSourceBadge")
            self.skill_source_value.setProperty("source", "builtin")
            self.skill_security_value = QLabel("Low security risk")
            self.skill_security_value.setObjectName("skillSecurityBadge")
            self.skill_security_value.setWordWrap(True)
            self.skill_security_value.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            overview_header.addWidget(self.skill_category_value)
            overview_header.addWidget(self.skill_source_value)
            overview_header.addStretch()
            self.skill_description = QLabel("")
            self.skill_description.setObjectName("skillDescription")
            self.skill_description.setWordWrap(True)
            overview_layout.addLayout(overview_header)
            overview_layout.addWidget(self.skill_security_value)
            self.skill_background_value = QLabel("")
            self.skill_background_value.setObjectName("activityHint")
            self.skill_background_value.setWordWrap(True)
            overview_layout.addWidget(self.skill_background_value)
            overview_layout.addWidget(self.skill_description)
            detail_layout.addWidget(overview)

            permission_heading = QLabel("REQUIRED PERMISSIONS")
            permission_heading.setObjectName("sectionLabel")
            detail_layout.addWidget(permission_heading)
            self.skill_permissions_summary = QLabel("")
            self.skill_permissions_summary.setWordWrap(True)
            detail_layout.addWidget(self.skill_permissions_summary)
            self.skill_permission_table = QTableWidget(0, 3)
            self.skill_permission_table.setObjectName("skillPermissionTable")
            self.skill_permission_table.setHorizontalHeaderLabels(
                ["Permission", "Applies to", "Why it is needed"]
            )
            self._configure_data_table(self.skill_permission_table)
            self.skill_permission_table.setWordWrap(True)
            self.skill_permission_table.setMinimumHeight(110)
            self.skill_permission_table.setMaximumHeight(130)
            self.skill_permission_table.horizontalHeader().setSectionResizeMode(
                2, QHeaderView.ResizeMode.Stretch
            )
            detail_layout.addWidget(self.skill_permission_table)

            demo_card = QFrame()
            demo_card.setObjectName("skillDetailCard")
            demo_layout = QVBoxLayout(demo_card)
            demo_layout.setContentsMargins(18, 16, 18, 16)
            demo_heading = QLabel("RUN A DEMO")
            demo_heading.setObjectName("sectionLabel")
            demo_explanation = self._info_label(
                "Edit the sample JSON, then run it through the normal permission and "
                "confirmation checks. The demo performs the real skill action."
            )
            self.skill_demo_input = QTextEdit()
            self.skill_demo_input.setObjectName("skillDemoInput")
            self.skill_demo_input.setAccessibleName("Skill demo JSON arguments")
            self.skill_demo_input.setMinimumHeight(90)
            self.skill_demo_input.setMaximumHeight(110)
            self.skill_demo_run_button = QPushButton("Run selected skill")
            self.skill_demo_run_button.setObjectName("primaryButton")
            self.skill_demo_run_button.clicked.connect(self._run_skill_demo)
            self.skill_demo_output = QTextEdit()
            self.skill_demo_output.setObjectName("skillDemoOutput")
            self.skill_demo_output.setAccessibleName("Skill demo result")
            self.skill_demo_output.setReadOnly(True)
            self.skill_demo_output.setMinimumHeight(110)
            self.skill_demo_output.setPlaceholderText("The demo result will appear here.")
            demo_layout.addWidget(demo_heading)
            demo_layout.addWidget(demo_explanation)
            demo_layout.addWidget(self.skill_demo_input)
            demo_layout.addWidget(self.skill_demo_run_button, 0, Qt.AlignmentFlag.AlignRight)
            demo_layout.addWidget(self.skill_demo_output)
            detail_layout.addWidget(demo_card)
            detail_layout.addStretch()
            detail_scroll.setWidget(detail)

            layout.addWidget(browser, 1)
            layout.addWidget(detail_scroll, 3)
            self._skill_demo_running = False
            self._reload_skills()
            return page

        @staticmethod
        def _skill_list_text(
            manifest: Any,
            enabled: bool,
            *,
            from_extension_hub: bool = False,
        ) -> str:
            state = "Enabled" if enabled else "Disabled"
            source = _skill_source_label(from_extension_hub)
            return f"{manifest.display_name}\n{source}  ·  {manifest.category}  ·  {state}"

        def _set_skill_source_badge(self, from_extension_hub: bool) -> None:
            self.skill_source_value.setText(_skill_source_label(from_extension_hub))
            self.skill_source_value.setProperty(
                "source", "extension_hub" if from_extension_hub else "builtin"
            )
            self.skill_source_value.setToolTip(
                "Installed through Extension Hub"
                if from_extension_hub
                else "Included with Local Assistant"
            )
            self.skill_source_value.style().unpolish(self.skill_source_value)
            self.skill_source_value.style().polish(self.skill_source_value)

        def _reload_skills(self) -> None:
            selected = self._selected_manifest()
            selected_id = selected.skill_id if selected is not None else "core.calculator"
            self.skill_list.clear()
            selected_item = None
            for group_index, (category, manifests) in enumerate(
                _group_skill_manifests(container.registry.list())
            ):
                accent, background = _skill_group_style(category, group_index)
                header = QListWidgetItem(f"{category}  ·  {len(manifests)} skills")
                header.setFlags(Qt.ItemFlag.ItemIsEnabled)
                header.setForeground(QBrush(QColor(accent)))
                header.setBackground(QBrush(QColor("#17263d")))
                header_font = header.font()
                header_font.setBold(True)
                header.setFont(header_font)
                header.setToolTip(f"Skills grouped under {category}")
                self.skill_list.addItem(header)
                for manifest in manifests:
                    from_extension_hub = container.extension_hub.is_installed(manifest.skill_id)
                    item = QListWidgetItem(
                        self._skill_list_text(
                            manifest,
                            container.registry.is_enabled(manifest.skill_id),
                            from_extension_hub=from_extension_hub,
                        )
                    )
                    item.setData(Qt.ItemDataRole.UserRole, manifest.skill_id)
                    item.setToolTip(f"{manifest.description}\n{manifest.skill_id}")
                    item.setForeground(QBrush(QColor("#dce7f3")))
                    item.setBackground(QBrush(QColor(background)))
                    self.skill_list.addItem(item)
                    if manifest.skill_id == selected_id:
                        selected_item = item
            if selected_item is None:
                selected_item = next(
                    (
                        self.skill_list.item(row)
                        for row in range(self.skill_list.count())
                        if self.skill_list.item(row).data(Qt.ItemDataRole.UserRole)
                    ),
                    None,
                )
            if selected_item is not None:
                self.skill_list.setCurrentItem(selected_item)
            self._update_skill_details()

        @staticmethod
        def _security_description(risk: RiskLevel) -> str:
            return {
                RiskLevel.LOW: "Low — no confirmation by default; permissions still apply.",
                RiskLevel.MEDIUM: "Medium — limited side effects or sensitive access; confirm to run.",
                RiskLevel.HIGH: "High — significant system or data changes; confirm to run.",
                RiskLevel.CRITICAL: "Critical — severe or irreversible impact; typed confirmation required.",
            }[risk]

        def _update_skill_details(self, *, reset_demo: bool = True) -> None:
            manifest = self._selected_manifest()
            if manifest is None:
                self.skill_detail_title.setText("No skill selected")
                self.skill_detail_id.clear()
                self._set_skill_source_badge(False)
                self.skill_description.clear()
                self.skill_background_value.clear()
                self.skill_permissions_summary.clear()
                self.skill_permission_table.setRowCount(0)
                self.skill_demo_input.clear()
                self.skill_demo_run_button.setEnabled(False)
                return

            enabled = container.registry.is_enabled(manifest.skill_id)
            self.skill_detail_title.setText(manifest.display_name)
            self.skill_detail_id.setText(
                f"{manifest.skill_id}  ·  version {manifest.version}  ·  "
                f"{'Enabled' if enabled else 'Disabled'}"
            )
            self.skill_category_value.setText(manifest.category)
            self._set_skill_source_badge(container.extension_hub.is_installed(manifest.skill_id))
            self.skill_security_value.setText(self._security_description(manifest.risk))
            self.skill_security_value.setProperty("level", manifest.risk.value)
            self.skill_security_value.style().unpolish(self.skill_security_value)
            self.skill_security_value.style().polish(self.skill_security_value)
            self.skill_background_value.setText(background_mode_summary(manifest))
            self.skill_description.setText(manifest.description)

            permissions = manifest.permissions
            self.skill_permission_table.setRowCount(max(1, len(permissions)))
            if not permissions:
                self.skill_permissions_summary.setText(
                    "This skill declares no special permissions."
                )
                for column, value in enumerate(
                    ("None", "Local skill boundary", "No protected capability is requested.")
                ):
                    self.skill_permission_table.setItem(0, column, self._table_item(value))
            else:
                self.skill_permissions_summary.setText(
                    f"{len(permissions)} declared permission"
                    f"{'s' if len(permissions) != 1 else ''}. Actual targets are checked "
                    "again when the demo runs."
                )
                for row, permission in enumerate(permissions):
                    values = (
                        capability_table_label(permission.capability),
                        target_summary(permission.targets),
                        permission.reason,
                    )
                    for column, value in enumerate(values):
                        self.skill_permission_table.setItem(row, column, self._table_item(value))
            self.skill_permission_table.resizeRowsToContents()

            if reset_demo:
                arguments = _demo_arguments(manifest.input_schema)
                self.skill_demo_input.setPlainText(json.dumps(arguments, indent=2))
                self.skill_demo_output.clear()
            self.skill_demo_run_button.setEnabled(enabled and not self._skill_demo_running)
            self.skill_demo_run_button.setToolTip(
                "" if enabled else "Enable this skill before running its demo."
            )

        def _toggle_skill(self) -> None:
            manifest = self._selected_manifest()
            if manifest is None:
                return
            if container.registry.is_enabled(manifest.skill_id):
                container.registry.disable(manifest.skill_id)
            else:
                container.registry.enable(manifest.skill_id)
            container.extension_hub.sync_managed_runtime_state()
            self.skill_list.currentItem().setText(
                self._skill_list_text(
                    manifest,
                    container.registry.is_enabled(manifest.skill_id),
                    from_extension_hub=container.extension_hub.is_installed(manifest.skill_id),
                )
            )
            self._update_skill_details(reset_demo=False)

        def _run_skill_demo(self) -> None:
            manifest = self._selected_manifest()
            if manifest is None or self._skill_demo_running:
                return
            if not container.registry.is_enabled(manifest.skill_id):
                self.skill_demo_output.setPlainText("Enable this skill before running its demo.")
                return
            try:
                arguments = json.loads(self.skill_demo_input.toPlainText() or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("Demo input must be a JSON object.")
                container.registry.inspect(manifest.skill_id).validate(arguments)
            except Exception as error:
                self.skill_demo_output.setPlainText(f"Invalid demo input:\n{error}")
                return

            call = SkillCall(
                skill_id=manifest.skill_id,
                caller="user",
                arguments=arguments,
                permission_context=PermissionContext(session_id="skills-tab-demo"),
            )
            self._set_skill_demo_running(True)
            self.skill_demo_output.setPlainText("Checking permissions and preparing the demo…")
            self._set_pipeline_state(
                PipelineState.THINKING, f"Preparing {manifest.display_name} demo"
            )
            worker = AsyncWorker(container.engine.prepare(call))
            worker.complete.connect(self._show_skill_demo_outcome)
            worker.failed.connect(self._show_skill_demo_error)
            worker.finished.connect(lambda: self._discard_worker(worker))
            self._workers.append(worker)
            worker.start()

        def _show_skill_demo_outcome(self, outcome: Any) -> None:
            if outcome.registration_requests:
                self.skill_demo_output.setPlainText(
                    "The demo needs an application registration before it can run."
                )
                self._request_registrations(
                    outcome,
                    continuation=self._show_skill_demo_outcome,
                    cancelled=lambda: self._cancel_skill_demo(
                        "Demo cancelled because the application was not registered."
                    ),
                )
            elif outcome.permission_requests:
                self.skill_demo_output.setPlainText(
                    "The demo is waiting for the required permission."
                )
                self._request_permissions(
                    outcome,
                    continuation=self._show_skill_demo_outcome,
                    cancelled=lambda: self._cancel_skill_demo(
                        "Demo cancelled because permission was not granted."
                    ),
                )
            elif outcome.confirmation:
                self.skill_demo_output.setPlainText(
                    "The demo is waiting for confirmation of the exact action."
                )
                self._request_confirmation(
                    outcome,
                    result_handler=self._show_skill_demo_result,
                    cancelled=lambda: self._cancel_skill_demo(
                        "Demo cancelled because the action was not confirmed."
                    ),
                )
            elif outcome.result is not None:
                self._show_skill_demo_result(outcome.result)
            else:
                self._show_skill_demo_error("The demo returned no result.")

        def _show_skill_demo_result(self, result: Any) -> None:
            response = response_from_result(
                result,
                detail=container.orchestrator.response_detail,
                allow_sensitive_speech=False,
                hide_sensitive_display=container.orchestrator.hide_sensitive_display,
            )
            self.skill_demo_output.setPlainText(
                f"Status: {result.status.value.replace('_', ' ').title()}\n\n"
                f"{response.display_text}"
            )
            self._set_skill_demo_running(False)
            state = (
                PipelineState.IDLE if result.status.value == "completed" else PipelineState.ERROR
            )
            self._set_pipeline_state(state, f"Demo {result.status.value.replace('_', ' ')}")

        def _show_skill_demo_error(self, error: str) -> None:
            self.skill_demo_output.setPlainText(f"Demo failed:\n{error}")
            self._set_skill_demo_running(False)
            self._set_pipeline_state(PipelineState.ERROR, "Skill demo failed")

        def _cancel_skill_demo(self, message: str) -> None:
            self.skill_demo_output.setPlainText(message)
            self._set_skill_demo_running(False)

        def _set_skill_demo_running(self, running: bool) -> None:
            self._skill_demo_running = running
            manifest = self._selected_manifest()
            enabled = bool(manifest and container.registry.is_enabled(manifest.skill_id))
            self.skill_demo_run_button.setEnabled(enabled and not running)
            self.skill_demo_run_button.setText("Running…" if running else "Run selected skill")

        def _install_skill(self) -> None:
            package = QFileDialog.getExistingDirectory(self, "Select Signed Skill Package")
            if not package:
                return
            try:
                manifest = container.registry.install(Path(package))
                container.registry.load_installed(manifest.skill_id)
            except Exception as error:
                QMessageBox.critical(self, "Skill Installation Failed", str(error))
                return
            self._reload_skills()
            QMessageBox.information(
                self,
                "Skill Installed",
                "The skill is installed disabled with no permissions.",
            )

        def _selected_manifest(self):
            item = self.skill_list.currentItem()
            if item is None:
                return None
            skill_id = item.data(Qt.ItemDataRole.UserRole)
            if not skill_id:
                return None
            return next(
                (
                    manifest
                    for manifest in container.registry.list()
                    if manifest.skill_id == skill_id
                ),
                None,
            )

        def _update_skill(self) -> None:
            selected = self._selected_manifest()
            if selected is None:
                return
            package = QFileDialog.getExistingDirectory(self, "Select Signed Skill Update")
            if not package:
                return
            try:
                try:
                    manifest, added = container.registry.update(Path(package))
                except PermissionError as expansion:
                    if not self._confirm(
                        f"{expansion}\n\nReview and accept the added capabilities?"
                    ):
                        return
                    manifest, added = container.registry.update(
                        Path(package), permission_reviewed=True
                    )
                container.registry.load_installed(manifest.skill_id)
            except Exception as error:
                QMessageBox.critical(self, "Skill Update Failed", str(error))
                return
            self._reload_skills()

        def _check_skill_health(self) -> None:
            selected = self._selected_manifest()
            if selected is None:
                return

            async def check():
                return await container.registry.inspect(selected.skill_id).health()

            worker = AsyncWorker(check())
            worker.complete.connect(
                lambda value: QMessageBox.information(
                    self, "Skill Health", json.dumps(value, indent=2)
                )
            )
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _uninstall_skill(self) -> None:
            selected = self._selected_manifest()
            if selected is None:
                return
            install_dir = container.registry.install_dir
            if not install_dir or not (install_dir / selected.skill_id).exists():
                QMessageBox.warning(
                    self, "Built-in Skill", "Built-in skills cannot be uninstalled."
                )
                return
            if self._confirm(f"Uninstall {selected.display_name}?"):
                container.registry.uninstall(selected.skill_id)
                self._reload_skills()

        def _permissions_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(
                self._info_label(
                    "Skills start with no sensitive permissions. Grants can be one-time, "
                    "session-only, always, or deny, and may be target-scoped. "
                    "Read-only dashboard providers appear here as Dashboard principals. "
                    "File access grants are managed in the File Access tab."
                )
            )
            self.permission_table = QTableWidget(0, 5)
            self.permission_table.setHorizontalHeaderLabels(
                ["Principal", "Capability", "Mode", "Targets", "Effect"]
            )
            self._configure_data_table(self.permission_table)
            self._reload_permissions()
            controls = QHBoxLayout()
            grant = QPushButton("Grant Permission")
            grant.clicked.connect(self._grant_permission)
            revoke = QPushButton("Revoke Selected")
            revoke.clicked.connect(self._revoke_permission)
            controls.addWidget(grant)
            controls.addWidget(revoke)
            layout.addWidget(self.permission_table)
            layout.addLayout(controls)
            return page

        def _reload_permissions(self) -> None:
            self.permission_table.setRowCount(0)
            for value in container.permissions.list_grants():
                if value["capability"] in {
                    "filesystem.read",
                    "filesystem.write",
                }:
                    continue
                row = self.permission_table.rowCount()
                self.permission_table.insertRow(row)
                try:
                    manifest = container.registry.get(value["skill_id"]).manifest
                    principal = (
                        f"{manifest.display_name} ({friendly_identifier(value['skill_id'])})"
                    )
                except KeyError:
                    principal = (
                        f"Dashboard ({friendly_identifier(value['skill_id'])})"
                        if value["skill_id"].startswith("dashboard.")
                        else friendly_identifier(value["skill_id"])
                    )
                fields = [
                    principal,
                    capability_table_label(value["capability"]),
                    grant_type_label(value["grant_type"]),
                    target_summary(value["targets"]),
                    value["effect"],
                ]
                for column, field in enumerate(fields):
                    item = self._table_item(field)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, value["skill_id"])
                    elif column == 1:
                        item.setData(Qt.ItemDataRole.UserRole, value["capability"])
                    self.permission_table.setItem(row, column, item)
            if hasattr(self, "file_access_grants_table"):
                self._reload_file_access()

        def _grant_permission(self) -> None:
            skill_choices = {
                skill_choice_label(item): item.skill_id for item in container.registry.list()
            }
            skill_label, ok = QInputDialog.getItem(
                self,
                "Grant Permission",
                "Skill",
                list(skill_choices),
                editable=False,
            )
            if not ok:
                return
            skill_id = skill_choices[skill_label]
            manifest = container.registry.get(skill_id).manifest
            capability_choices = {
                capability_choice_label(capability): capability
                for capability in available_capabilities(manifest.permissions)
                if capability
                not in {
                    "filesystem.read",
                    "filesystem.write",
                }
            }
            if not capability_choices:
                QMessageBox.information(
                    self,
                    "No Permissions Available",
                    "This skill only exposes file access permissions. Use the File Access tab.",
                )
                return
            capability_label, ok = QInputDialog.getItem(
                self,
                "Grant Permission",
                "Capability",
                list(capability_choices),
                editable=False,
            )
            if not ok or not capability_label:
                return
            capability = capability_choices[capability_label]
            target_mode, ok = QInputDialog.getItem(
                self,
                "Grant Permission",
                "Targets",
                [
                    "Any target",
                    "One exact target or approved folder",
                ],
                editable=False,
            )
            if not ok:
                return
            target = ""
            if target_mode != "Any target":
                target, ok = QInputDialog.getText(
                    self,
                    "Grant Permission",
                    "Exact target, website, application, or approved folder",
                )
                if not ok:
                    return
            mode, ok = QInputDialog.getItem(
                self,
                "Grant Permission",
                "Grant mode",
                [
                    "Just once",
                    "This session",
                    "Always",
                    "Deny",
                ],
                editable=False,
            )
            if not ok:
                return
            modes = {
                "Just once": GrantType.ONCE,
                "This session": GrantType.SESSION,
                "Always": GrantType.ALWAYS,
                "Deny": GrantType.DENY,
            }
            grant_type = modes[mode]
            container.permissions.grant(
                user_id="local-user",
                skill_id=skill_id,
                capability=capability,
                grant_type=grant_type,
                targets=(target,) if target else (),
                session_id="desktop" if grant_type == GrantType.SESSION else None,
                correlation_id="desktop-permission-manager",
            )
            self._reload_permissions()

        def _revoke_permission(self) -> None:
            row = self.permission_table.currentRow()
            if row < 0:
                return
            container.permissions.revoke(
                "local-user",
                self.permission_table.item(row, 0).data(Qt.ItemDataRole.UserRole),
                self.permission_table.item(row, 1).data(Qt.ItemDataRole.UserRole),
                "desktop-permission-manager",
            )
            self._reload_permissions()

        def _file_access_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(
                self._info_label(
                    "File access is controlled by skill grants plus global path "
                    "allow/block rules. Block rules take precedence over grants."
                )
            )

            grant_title = QLabel("Filesystem grants")
            grant_title.setObjectName("sectionTitle")
            layout.addWidget(grant_title)
            self.file_access_grants_table = QTableWidget(0, 5)
            self.file_access_grants_table.setHorizontalHeaderLabels(
                ["Skill", "Access", "Mode", "Targets", "Effect"]
            )
            self._configure_data_table(self.file_access_grants_table)
            layout.addWidget(self.file_access_grants_table)

            grant_controls = QHBoxLayout()
            grant_access = QPushButton("Grant File Access")
            grant_access.clicked.connect(self._grant_file_access)
            revoke_grant = QPushButton("Revoke Selected Grant")
            revoke_grant.clicked.connect(self._revoke_file_access_grant)
            refresh = QPushButton("Refresh")
            refresh.clicked.connect(self._reload_file_access)
            grant_controls.addWidget(grant_access)
            grant_controls.addWidget(revoke_grant)
            grant_controls.addWidget(refresh)
            layout.addLayout(grant_controls)

            policy_title = QLabel("Global path policy")
            policy_title.setObjectName("sectionTitle")
            layout.addWidget(policy_title)
            self.file_access_policy_table = QTableWidget(0, 3)
            self.file_access_policy_table.setHorizontalHeaderLabels(["Access", "Rule", "Path"])
            self._configure_data_table(self.file_access_policy_table)
            layout.addWidget(self.file_access_policy_table)

            policy_controls = QHBoxLayout()
            add_allow = QPushButton("Add Allow Rule")
            add_allow.clicked.connect(lambda: self._add_file_access_policy_rule("allow"))
            add_block = QPushButton("Add Block Rule")
            add_block.clicked.connect(lambda: self._add_file_access_policy_rule("block"))
            remove_rule = QPushButton("Remove Selected Rule")
            remove_rule.clicked.connect(self._remove_file_access_policy_rule)
            policy_controls.addWidget(add_allow)
            policy_controls.addWidget(add_block)
            policy_controls.addWidget(remove_rule)
            layout.addLayout(policy_controls)

            self._reload_file_access()
            return page

        def _reload_file_access(self) -> None:
            self.file_access_grants_table.setRowCount(0)
            for value in container.permissions.list_grants():
                if value["capability"] not in {
                    "filesystem.read",
                    "filesystem.write",
                }:
                    continue
                row = self.file_access_grants_table.rowCount()
                self.file_access_grants_table.insertRow(row)
                manifest = container.registry.get(value["skill_id"]).manifest
                fields = [
                    f"{manifest.display_name} ({friendly_identifier(value['skill_id'])})",
                    capability_table_label(value["capability"]),
                    grant_type_label(value["grant_type"]),
                    target_summary(value["targets"]),
                    value["effect"],
                ]
                for column, field in enumerate(fields):
                    item = self._table_item(field)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, value["skill_id"])
                    elif column == 1:
                        item.setData(Qt.ItemDataRole.UserRole, value["capability"])
                    self.file_access_grants_table.setItem(row, column, item)

            self.file_access_policy_table.setRowCount(0)
            policy = container.file_access_policy.get()
            rows = (
                ("Read", "Allow", "read_whitelist", policy.read_whitelist),
                ("Read", "Block", "read_blacklist", policy.read_blacklist),
                ("Write/delete", "Allow", "write_whitelist", policy.write_whitelist),
                ("Write/delete", "Block", "write_blacklist", policy.write_blacklist),
            )
            for access, rule, field, paths in rows:
                for path in paths:
                    row = self.file_access_policy_table.rowCount()
                    self.file_access_policy_table.insertRow(row)
                    fields = [access, rule, path]
                    for column, value in enumerate(fields):
                        item = self._table_item(value)
                        if column == 0:
                            item.setData(Qt.ItemDataRole.UserRole, field)
                        elif column == 2:
                            item.setData(Qt.ItemDataRole.UserRole, path)
                        self.file_access_policy_table.setItem(row, column, item)

        def _revoke_file_access_grant(self) -> None:
            row = self.file_access_grants_table.currentRow()
            if row < 0:
                return
            container.permissions.revoke(
                "local-user",
                self.file_access_grants_table.item(row, 0).data(Qt.ItemDataRole.UserRole),
                self.file_access_grants_table.item(row, 1).data(Qt.ItemDataRole.UserRole),
                "desktop-file-access-manager",
            )
            self._reload_permissions()

        def _grant_file_access(self) -> None:
            skill_choices = {}
            for item in container.registry.list():
                capabilities = available_capabilities(item.permissions)
                if {"filesystem.read", "filesystem.write"} & set(capabilities):
                    skill_choices[skill_choice_label(item)] = item.skill_id
            if not skill_choices:
                QMessageBox.information(
                    self,
                    "No File Access Skills",
                    "No skills declare file access permissions.",
                )
                return
            skill_label, ok = QInputDialog.getItem(
                self,
                "Grant File Access",
                "Skill",
                list(skill_choices),
                editable=False,
            )
            if not ok:
                return
            skill_id = skill_choices[skill_label]
            manifest = container.registry.get(skill_id).manifest
            capability_choices = {
                capability_choice_label(capability): capability
                for capability in available_capabilities(manifest.permissions)
                if capability
                in {
                    "filesystem.read",
                    "filesystem.write",
                }
            }
            capability_label, ok = QInputDialog.getItem(
                self,
                "Grant File Access",
                "Access",
                list(capability_choices),
                editable=False,
            )
            if not ok or not capability_label:
                return
            capability = capability_choices[capability_label]
            target_mode, ok = QInputDialog.getItem(
                self,
                "Grant File Access",
                "Scope",
                [
                    "Any path",
                    "One exact file or approved folder",
                ],
                editable=False,
            )
            if not ok:
                return
            target = ""
            if target_mode != "Any path":
                target, ok = QInputDialog.getText(
                    self,
                    "Grant File Access",
                    "Exact file path or approved folder",
                )
                if not ok:
                    return
            mode, ok = QInputDialog.getItem(
                self,
                "Grant File Access",
                "Grant mode",
                [
                    "Just once",
                    "This session",
                    "Always",
                    "Deny",
                ],
                editable=False,
            )
            if not ok:
                return
            modes = {
                "Just once": GrantType.ONCE,
                "This session": GrantType.SESSION,
                "Always": GrantType.ALWAYS,
                "Deny": GrantType.DENY,
            }
            grant_type = modes[mode]
            container.permissions.grant(
                user_id="local-user",
                skill_id=skill_id,
                capability=capability,
                grant_type=grant_type,
                targets=(target,) if target else (),
                session_id="desktop" if grant_type == GrantType.SESSION else None,
                correlation_id="desktop-file-access-manager",
            )
            self._reload_permissions()

        def _add_file_access_policy_rule(self, rule: str) -> None:
            access_label, ok = QInputDialog.getItem(
                self,
                "Add File Access Rule",
                "Access",
                ["Read", "Write/delete"],
                editable=False,
            )
            if not ok:
                return
            path, ok = QInputDialog.getText(
                self,
                "Add File Access Rule",
                "Path or folder",
            )
            if not ok or not path.strip():
                return
            container.file_access_policy.update_entry(
                list_name="whitelist" if rule == "allow" else "blacklist",
                access="write" if access_label == "Write/delete" else "read",
                path=path.strip(),
                add=True,
            )
            self._reload_file_access()

        def _remove_file_access_policy_rule(self) -> None:
            row = self.file_access_policy_table.currentRow()
            if row < 0:
                return
            field = self.file_access_policy_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            path = self.file_access_policy_table.item(row, 2).data(Qt.ItemDataRole.UserRole)
            access = "write" if field.startswith("write_") else "read"
            list_name = "whitelist" if field.endswith("_whitelist") else "blacklist"
            container.file_access_policy.update_entry(
                list_name=list_name,
                access=access,
                path=path,
                add=False,
            )
            self._reload_file_access()

        def _audit_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            table = QTableWidget(0, 4)
            table.setObjectName("auditTable")
            table.setAccessibleName("Audit log entries")
            table.setHorizontalHeaderLabels(["Time", "Event", "Skill", "Decision"])
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setAlternatingRowColors(True)
            table.setWordWrap(False)
            for row_data in container.audit.query():
                row = table.rowCount()
                table.insertRow(row)
                for column, value in enumerate(
                    [
                        row_data["timestamp"],
                        row_data["event_type"],
                        row_data["skill_id"],
                        row_data["decision"],
                    ]
                ):
                    table.setItem(row, column, QTableWidgetItem(str(value or "")))
            header = table.horizontalHeader()
            for column in range(table.columnCount()):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            self.audit_table = table
            layout.addWidget(table)
            return page

        def _settings_tab(self) -> QWidget:
            page = QScrollArea()
            page.setWidgetResizable(True)
            content = QWidget()
            form = QFormLayout(content)
            self.setting_offline = QCheckBox()
            self.setting_offline.setChecked(container.settings.offline_lock)
            self.setting_scheduler_enabled = QCheckBox()
            self.setting_scheduler_enabled.setChecked(container.settings.scheduler_enabled)
            self.setting_background_actions = QCheckBox()
            self.setting_background_actions.setChecked(
                container.settings.background_actions_enabled
            )
            self.setting_pause_while_locked = QCheckBox()
            self.setting_pause_while_locked.setChecked(
                container.settings.pause_background_while_locked
            )
            self.setting_notify_background_success = QCheckBox()
            self.setting_notify_background_success.setChecked(
                container.settings.notify_on_background_success
            )
            self.setting_notify_background_failure = QCheckBox()
            self.setting_notify_background_failure.setChecked(
                container.settings.notify_on_background_failure
            )
            self.setting_show_in_app_notifications = QCheckBox()
            self.setting_show_in_app_notifications.setChecked(
                container.settings.show_in_app_notifications
            )
            self.setting_show_system_notifications = QCheckBox()
            self.setting_show_system_notifications.setChecked(
                container.settings.show_system_notifications
            )
            self.setting_quiet_hours_enabled = QCheckBox()
            self.setting_quiet_hours_enabled.setChecked(
                container.settings.notification_quiet_hours_enabled
            )
            quiet_hours = QWidget()
            quiet_hours_layout = QHBoxLayout(quiet_hours)
            quiet_hours_layout.setContentsMargins(0, 0, 0, 0)
            self.setting_quiet_hours_start = QLineEdit(
                container.settings.notification_quiet_hours_start
            )
            self.setting_quiet_hours_start.setPlaceholderText("22:00")
            self.setting_quiet_hours_end = QLineEdit(
                container.settings.notification_quiet_hours_end
            )
            self.setting_quiet_hours_end.setPlaceholderText("07:00")
            quiet_hours_layout.addWidget(self.setting_quiet_hours_start)
            quiet_hours_layout.addWidget(QLabel("to"))
            quiet_hours_layout.addWidget(self.setting_quiet_hours_end)
            self.setting_default_timezone = QLineEdit(container.settings.default_timezone)
            self.setting_stt = QLineEdit(container.settings.stt_model)
            self.setting_llm = QLineEdit(container.settings.llm_model)
            self.setting_llm_keep_alive = QLineEdit(container.settings.llm_keep_alive)
            self.setting_llm_keep_alive.setPlaceholderText("30m")
            self.setting_llm_preload = QCheckBox()
            self.setting_llm_preload.setChecked(container.settings.llm_preload)
            self.setting_openai_model = QLineEdit(container.settings.openai_model)
            self.setting_openai_codex_model = QLineEdit(container.settings.openai_codex_model)
            self.setting_anthropic_model = QLineEdit(container.settings.anthropic_model)
            vault = SecretVault(container.settings.data_dir / "secrets")
            self.setting_cloud_model_help = QLabel(
                "Connect OpenAI or Anthropic from the Extension Hub; API keys are managed "
                "there and never stored in application settings. The OpenAI extension also "
                "provides local Codex mode without an OpenAI API key."
            )
            self.setting_cloud_model_help.setWordWrap(True)
            self.setting_cloud_model_help.setObjectName("activityHint")
            self.setting_cloud_pricing = QLineEdit(
                json.dumps(container.settings.cloud_pricing, sort_keys=True)
            )
            self.setting_cloud_pricing.setPlaceholderText(
                '{"openai:gpt-5.5":{"input_usd_per_million":1.25,"output_usd_per_million":10}}'
            )
            self.setting_cloud_pricing.setToolTip(
                "JSON pricing keyed by provider:model; prices are USD per million tokens."
            )
            self.setting_brave_key = QLineEdit()
            self.setting_brave_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.setting_brave_key.setPlaceholderText(
                "Stored; enter a new key to replace"
                if vault.has("brave")
                else "Brave Search API key"
            )
            self.setting_search_provider = QComboBox()
            self.setting_search_provider.addItem("DuckDuckGo Instant Answer", "duckduckgo")
            self.setting_search_provider.addItem("Brave Search", "brave")
            selected = self.setting_search_provider.findData(container.settings.search_provider)
            self.setting_search_provider.setCurrentIndex(max(0, selected))
            self.setting_extension_catalog_url = QLineEdit(
                container.settings.extension_catalog_url or ""
            )
            self.setting_extension_catalog_url.setPlaceholderText(
                "https://extensions.example.com/v1/catalog.json"
            )
            self.setting_tts_provider = QComboBox()
            self.setting_tts_provider.addItem("Piper (local)", "piper")
            self.setting_tts_provider.addItem("ElevenLabs (cloud)", "elevenlabs")
            selected = self.setting_tts_provider.findData(container.settings.tts_provider)
            self.setting_tts_provider.setCurrentIndex(max(0, selected))
            self.setting_piper_executable = QLineEdit(
                str(container.settings.piper_executable or "")
            )
            self.setting_piper_model = QLineEdit(str(container.settings.piper_model or ""))
            self.setting_piper_executable_label = QLabel("Piper executable")
            self.setting_piper_model_label = QLabel("Piper voice model")
            detected_executable = find_piper_executable(container.settings.data_dir)
            if detected_executable and not container.settings.piper_executable:
                self.setting_piper_executable.setPlaceholderText(f"Detected: {detected_executable}")
            detected_voices = find_piper_voice_models(container.settings.data_dir)
            if detected_voices and not container.settings.piper_model:
                self.setting_piper_model.setPlaceholderText(f"Detected: {detected_voices[0]}")
            piper_executable_row = QWidget()
            piper_executable_layout = QHBoxLayout(piper_executable_row)
            piper_executable_layout.setContentsMargins(0, 0, 0, 0)
            piper_executable_layout.addWidget(self.setting_piper_executable, 1)
            piper_executable_browse = QPushButton("Browse")
            piper_executable_browse.clicked.connect(self._select_piper_executable)
            piper_executable_layout.addWidget(piper_executable_browse)
            self.setting_piper_executable_row = piper_executable_row
            piper_model_row = QWidget()
            piper_model_layout = QHBoxLayout(piper_model_row)
            piper_model_layout.setContentsMargins(0, 0, 0, 0)
            piper_model_layout.addWidget(self.setting_piper_model, 1)
            piper_model_browse = QPushButton("Browse")
            piper_model_browse.clicked.connect(self._select_piper_model)
            piper_model_layout.addWidget(piper_model_browse)
            self.setting_piper_model_row = piper_model_row
            self.setting_piper_help = QLabel(
                f"Found {len(detected_voices)} local Piper voice file(s). "
                "Browse opens the detected model folder."
                if detected_voices
                else "Choose the Piper .onnx voice file; Browse checks common model folders."
            )
            self.setting_piper_help.setWordWrap(True)
            self.setting_elevenlabs_key = QLineEdit()
            self.setting_elevenlabs_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.setting_elevenlabs_key.setPlaceholderText(
                "Stored; enter a new key to replace"
                if vault.has("elevenlabs")
                else "ElevenLabs API key"
            )
            self.setting_elevenlabs_voice_id = QLineEdit(
                container.settings.elevenlabs_voice_id or ""
            )
            self.setting_elevenlabs_voice_id.setPlaceholderText(
                "Copy the voice ID from ElevenLabs My Voices"
            )
            self.setting_elevenlabs_model = QComboBox()
            self.setting_elevenlabs_model.setEditable(True)
            self.setting_elevenlabs_model.addItems(
                [
                    "eleven_flash_v2_5",
                    "eleven_multilingual_v2",
                    "eleven_v3",
                ]
            )
            self.setting_elevenlabs_model.setCurrentText(container.settings.elevenlabs_model_id)
            self.setting_elevenlabs_key_label = QLabel("ElevenLabs API key")
            self.setting_elevenlabs_voice_id_label = QLabel("ElevenLabs voice ID")
            self.setting_elevenlabs_model_label = QLabel("ElevenLabs model")
            self.setting_elevenlabs_help = QLabel(
                "ElevenLabs uses cloud voice/model IDs rather than local files and is "
                "disabled while the global offline lock is on."
            )
            self.setting_elevenlabs_help.setWordWrap(True)
            self.setting_speech = QComboBox()
            self.setting_speech.addItems(["always", "voice_only", "never"])
            self.setting_speech.setCurrentText(container.settings.speech_mode)
            self.setting_voice_activation_mode = QComboBox()
            self.setting_voice_activation_mode.addItem("Hotkey only", "hotkey")
            self.setting_voice_activation_mode.addItem("Keyword or clap", "keyword_clap")
            self.setting_voice_activation_mode.addItem("Persistent voice", "persistent")
            selected = self.setting_voice_activation_mode.findData(
                container.settings.voice_activation_mode
            )
            self.setting_voice_activation_mode.setCurrentIndex(max(0, selected))
            self.setting_wake_word_enabled = QCheckBox()
            self.setting_wake_word_enabled.setChecked(container.settings.wake_word_enabled)
            self.setting_wake_word_enabled_label = QLabel("Enable model wake word")
            self.setting_wake_word_model = QLineEdit(str(container.settings.wake_word_model or ""))
            self.setting_wake_word_model.setPlaceholderText("Select an openWakeWord ONNX model")
            self.setting_wake_word_model_label = QLabel("Wake-word ONNX model")
            wake_word_model_row = QWidget()
            wake_word_model_layout = QHBoxLayout(wake_word_model_row)
            wake_word_model_layout.setContentsMargins(0, 0, 0, 0)
            wake_word_model_layout.addWidget(self.setting_wake_word_model, 1)
            wake_word_browse = QPushButton("Browse")
            wake_word_browse.clicked.connect(self._select_wake_word_model)
            wake_word_model_layout.addWidget(wake_word_browse)
            self.setting_wake_word_model_row = wake_word_model_row
            self.setting_wake_word_threshold = QLineEdit(
                str(container.settings.wake_word_threshold)
            )
            self.setting_wake_word_threshold_label = QLabel("Wake-word sensitivity threshold")
            self.setting_voice_keyword_enabled = QCheckBox()
            self.setting_voice_keyword_enabled.setChecked(
                container.settings.voice_activation_keyword_enabled
            )
            self.setting_voice_keyword = QLineEdit(container.settings.voice_activation_keyword)
            self.setting_voice_keyword_enabled_label = QLabel("Enable activation keyword")
            self.setting_voice_keyword_label = QLabel("Activation keyword")
            self.setting_voice_clap_enabled = QCheckBox()
            self.setting_voice_clap_enabled.setChecked(
                container.settings.voice_activation_clap_enabled
            )
            self.setting_voice_clap_threshold = QLineEdit(
                str(container.settings.voice_activation_clap_threshold)
            )
            self.setting_voice_clap_enabled_label = QLabel("Enable clap activation")
            self.setting_voice_clap_threshold_label = QLabel("Clap sensitivity threshold")
            self.setting_input = QComboBox()
            self.setting_input.addItem("System default", None)
            for device in display_audio_devices(input_only=True):
                self.setting_input.addItem(device.label, device.index)
            selected = self.setting_input.findData(container.settings.audio_input_device)
            self.setting_input.setCurrentIndex(max(0, selected))
            self.setting_ptt = QLineEdit(container.settings.push_to_talk_hotkey)
            self.setting_cancel = QLineEdit(container.settings.cancel_hotkey)
            self.setting_retention = QLineEdit(str(container.settings.retain_transcripts_days))
            self.setting_dashboard_history_retention = QLineEdit(
                str(container.settings.dashboard_history_retention_days)
            )
            self.setting_diagnostic_audio = QCheckBox()
            self.setting_diagnostic_audio.setChecked(container.settings.diagnostic_audio)
            self.setting_noise = QCheckBox()
            self.setting_noise.setChecked(container.settings.noise_suppression)
            self.setting_gain = QCheckBox()
            self.setting_gain.setChecked(container.settings.automatic_gain_control)
            self.setting_detail = QComboBox()
            self.setting_detail.addItems(["short", "normal", "detailed"])
            self.setting_detail.setCurrentText(container.settings.response_detail)
            self.setting_text_scale = QComboBox()
            for label, value in (
                ("100%", 1.0),
                ("125%", 1.25),
                ("150%", 1.5),
                ("175%", 1.75),
            ):
                self.setting_text_scale.addItem(label, value)
            selected = self.setting_text_scale.findData(container.settings.ui_text_scale)
            self.setting_text_scale.setCurrentIndex(max(0, selected))
            self.setting_text_scale.setAccessibleName("Interface text size")
            self.setting_high_contrast = QCheckBox()
            self.setting_high_contrast.setChecked(container.settings.ui_high_contrast)
            self.setting_high_contrast.setAccessibleName("Use high contrast colors")
            self.setting_reduced_motion = QCheckBox()
            self.setting_reduced_motion.setChecked(container.settings.ui_reduced_motion)
            self.setting_reduced_motion.setAccessibleName("Reduce interface motion")
            self.setting_show_reasoning = QCheckBox()
            self.setting_show_reasoning.setChecked(container.settings.show_reasoning_steps)
            self.setting_terminal_debug = QCheckBox()
            self.setting_terminal_debug.setChecked(container.settings.terminal_debug)
            self.setting_hide_sensitive = QCheckBox()
            self.setting_hide_sensitive.setChecked(container.settings.hide_sensitive_display)
            self.setting_confirmation_prompts = QTableWidget(len(confirmation_prompt_actions), 2)
            self.setting_confirmation_prompts.setHorizontalHeaderLabels(["Action", "Prompt"])
            self.setting_confirmation_prompts.setEditTriggers(
                QTableWidget.EditTrigger.NoEditTriggers
            )
            self.setting_confirmation_prompt_rows = []
            for row, (key, label, default) in enumerate(confirmation_prompt_actions):
                checked = container.settings.confirmation_overrides.get(key, default)
                self.setting_confirmation_prompts.setItem(row, 0, QTableWidgetItem(label))
                checkbox = QCheckBox()
                checkbox.setChecked(checked)
                checkbox.setAccessibleName(f"Require prompt for {label}")
                self.setting_confirmation_prompts.setCellWidget(row, 1, checkbox)
                self.setting_confirmation_prompt_rows.append((key, default, checkbox))
            self.setting_confirmation_prompts.horizontalHeader().setStretchLastSection(True)
            self.setting_confirmation_prompts.setMinimumHeight(260)
            save = QPushButton("Save Settings")
            save.clicked.connect(self._save_settings)
            form.addRow("Global offline lock", self.setting_offline)
            form.addRow("Extension catalog URL", self.setting_extension_catalog_url)
            form.addRow("Enable durable scheduling", self.setting_scheduler_enabled)
            form.addRow("Enable background skill actions", self.setting_background_actions)
            form.addRow("Pause interactive actions while locked", self.setting_pause_while_locked)
            form.addRow(
                "Notify when background work completes",
                self.setting_notify_background_success,
            )
            form.addRow(
                "Notify when background work fails",
                self.setting_notify_background_failure,
            )
            form.addRow("Show in-app notification banners", self.setting_show_in_app_notifications)
            form.addRow("Show Windows notifications", self.setting_show_system_notifications)
            form.addRow("Enable notification quiet hours", self.setting_quiet_hours_enabled)
            form.addRow("Quiet hours (24-hour time)", quiet_hours)
            form.addRow("Default schedule time zone", self.setting_default_timezone)
            form.addRow("Input device", self.setting_input)
            form.addRow("STT model", self.setting_stt)
            form.addRow("Local LLM model", self.setting_llm)
            form.addRow("Local LLM keep-alive", self.setting_llm_keep_alive)
            form.addRow("Preload local LLM at startup", self.setting_llm_preload)
            form.addRow("OpenAI model", self.setting_openai_model)
            form.addRow("OpenAI Codex model", self.setting_openai_codex_model)
            form.addRow("Anthropic model", self.setting_anthropic_model)
            form.addRow("Cloud API keys", self.setting_cloud_model_help)
            form.addRow("Cloud pricing JSON", self.setting_cloud_pricing)
            form.addRow("Web search provider", self.setting_search_provider)
            form.addRow("Brave Search API key", self.setting_brave_key)
            form.addRow("Speech provider", self.setting_tts_provider)
            form.addRow(
                self.setting_piper_executable_label,
                self.setting_piper_executable_row,
            )
            form.addRow(self.setting_piper_model_label, self.setting_piper_model_row)
            form.addRow("", self.setting_piper_help)
            form.addRow(self.setting_elevenlabs_key_label, self.setting_elevenlabs_key)
            form.addRow(
                self.setting_elevenlabs_voice_id_label,
                self.setting_elevenlabs_voice_id,
            )
            form.addRow(
                self.setting_elevenlabs_model_label,
                self.setting_elevenlabs_model,
            )
            form.addRow("", self.setting_elevenlabs_help)
            form.addRow("Speech mode", self.setting_speech)
            form.addRow("Voice activation", self.setting_voice_activation_mode)
            form.addRow(
                self.setting_wake_word_enabled_label,
                self.setting_wake_word_enabled,
            )
            form.addRow(self.setting_wake_word_model_label, self.setting_wake_word_model_row)
            form.addRow(
                self.setting_wake_word_threshold_label,
                self.setting_wake_word_threshold,
            )
            form.addRow(
                self.setting_voice_keyword_enabled_label,
                self.setting_voice_keyword_enabled,
            )
            form.addRow(self.setting_voice_keyword_label, self.setting_voice_keyword)
            form.addRow(
                self.setting_voice_clap_enabled_label,
                self.setting_voice_clap_enabled,
            )
            form.addRow(
                self.setting_voice_clap_threshold_label,
                self.setting_voice_clap_threshold,
            )
            form.addRow("Push-to-talk hotkey", self.setting_ptt)
            form.addRow("Cancel hotkey", self.setting_cancel)
            form.addRow("Transcript retention days", self.setting_retention)
            form.addRow(
                "Dashboard numeric history days",
                self.setting_dashboard_history_retention,
            )
            form.addRow("Diagnostic raw audio", self.setting_diagnostic_audio)
            form.addRow("Noise suppression", self.setting_noise)
            form.addRow("Automatic gain control", self.setting_gain)
            form.addRow("Response detail", self.setting_detail)
            form.addRow("Interface text size", self.setting_text_scale)
            form.addRow("High contrast", self.setting_high_contrast)
            form.addRow("Reduce motion", self.setting_reduced_motion)
            form.addRow("Show request breakdown", self.setting_show_reasoning)
            form.addRow("Terminal debug output", self.setting_terminal_debug)
            form.addRow("Hide sensitive display results", self.setting_hide_sensitive)
            form.addRow("Require yes/no prompts", self.setting_confirmation_prompts)
            form.addRow(save)
            self.setting_speech.currentTextChanged.connect(
                lambda _text: self._update_voice_settings_visibility()
            )
            self.setting_tts_provider.currentIndexChanged.connect(
                lambda _index: self._update_voice_settings_visibility()
            )
            self.setting_voice_activation_mode.currentIndexChanged.connect(
                lambda _index: self._update_voice_settings_visibility()
            )
            self.setting_wake_word_enabled.toggled.connect(
                lambda _checked: self._update_voice_settings_visibility()
            )
            self.setting_voice_keyword_enabled.toggled.connect(
                lambda _checked: self._update_voice_settings_visibility()
            )
            self.setting_voice_clap_enabled.toggled.connect(
                lambda _checked: self._update_voice_settings_visibility()
            )
            self._update_voice_settings_visibility()
            page.setWidget(content)
            return page

        def _update_voice_settings_visibility(self) -> None:
            speech_enabled = self.setting_speech.currentText() != "never"
            provider = self.setting_tts_provider.currentData()
            piper_active = speech_enabled and provider == "piper"
            elevenlabs_active = speech_enabled and provider == "elevenlabs"
            self.setting_piper_executable_label.setVisible(piper_active)
            self.setting_piper_executable_row.setVisible(piper_active)
            self.setting_piper_model_label.setVisible(piper_active)
            self.setting_piper_model_row.setVisible(piper_active)
            self.setting_piper_help.setVisible(piper_active)
            self.setting_elevenlabs_key_label.setVisible(elevenlabs_active)
            self.setting_elevenlabs_key.setVisible(elevenlabs_active)
            self.setting_elevenlabs_voice_id_label.setVisible(elevenlabs_active)
            self.setting_elevenlabs_voice_id.setVisible(elevenlabs_active)
            self.setting_elevenlabs_model_label.setVisible(elevenlabs_active)
            self.setting_elevenlabs_model.setVisible(elevenlabs_active)
            self.setting_elevenlabs_help.setVisible(elevenlabs_active)

            activation_mode = self.setting_voice_activation_mode.currentData()
            keyword_clap_active = activation_mode == "keyword_clap"
            keyword_active = keyword_clap_active and self.setting_voice_keyword_enabled.isChecked()
            clap_active = keyword_clap_active and self.setting_voice_clap_enabled.isChecked()

            self.setting_voice_keyword_enabled_label.setVisible(keyword_clap_active)
            self.setting_voice_keyword_enabled.setVisible(keyword_clap_active)
            self.setting_voice_keyword_label.setVisible(keyword_active)
            self.setting_voice_keyword.setVisible(keyword_active)
            self.setting_voice_clap_enabled_label.setVisible(keyword_clap_active)
            self.setting_voice_clap_enabled.setVisible(keyword_clap_active)
            self.setting_voice_clap_threshold_label.setVisible(clap_active)
            self.setting_voice_clap_threshold.setVisible(clap_active)

            wake_word_active = self.setting_wake_word_enabled.isChecked()
            self.setting_wake_word_model_label.setVisible(wake_word_active)
            self.setting_wake_word_model_row.setVisible(wake_word_active)
            self.setting_wake_word_threshold_label.setVisible(wake_word_active)
            self.setting_wake_word_threshold.setVisible(wake_word_active)

        def _select_piper_executable(self) -> None:
            configured_text = self.setting_piper_executable.text().strip()
            configured = Path(configured_text) if configured_text else None
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Piper Executable",
                str(
                    piper_executable_dialog_directory(
                        container.settings.data_dir,
                        configured_executable=configured,
                    )
                ),
                "Piper executable (piper.exe);;Applications (*.exe);;All files (*)",
            )
            if path:
                self.setting_piper_executable.setText(path)

        def _select_piper_model(self) -> None:
            configured_text = self.setting_piper_model.text().strip()
            configured = Path(configured_text) if configured_text else None
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Piper Voice Model",
                str(
                    piper_model_dialog_directory(
                        container.settings.data_dir,
                        configured_model=configured,
                    )
                ),
                "Piper voice models (*.onnx);;All files (*)",
            )
            if path:
                self.setting_piper_model.setText(path)

        def _select_wake_word_model(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Wake-word ONNX Model",
                str(container.settings.wake_word_model or container.settings.data_dir),
                "ONNX models (*.onnx)",
            )
            if path:
                self.setting_wake_word_model.setText(path)

        def _save_settings(self) -> None:
            try:
                speech_enabled = self.setting_speech.currentText() != "never"
                cloud_pricing_text = self.setting_cloud_pricing.text().strip()
                cloud_pricing = json.loads(cloud_pricing_text or "{}")
                if not isinstance(cloud_pricing, dict):
                    raise ValueError("Cloud pricing must be a JSON object")
                tts_provider = self.setting_tts_provider.currentData()
                piper_executable_text = self.setting_piper_executable.text().strip()
                piper_executable = (
                    Path(piper_executable_text).expanduser() if piper_executable_text else None
                )
                piper_model_text = self.setting_piper_model.text().strip()
                piper_model = Path(piper_model_text).expanduser() if piper_model_text else None
                if (
                    speech_enabled
                    and tts_provider == "piper"
                    and (piper_executable is not None or piper_model is not None)
                ):
                    if piper_executable is None or not piper_executable.is_file():
                        raise ValueError("Select an existing Piper executable")
                    if piper_model is None or not piper_model.is_file():
                        raise ValueError("Select an existing Piper voice model")
                    if piper_model.suffix.casefold() != ".onnx":
                        raise ValueError("Piper voice model must be an .onnx file")
                    piper_config = piper_model.with_suffix(f"{piper_model.suffix}.json")
                    if not piper_config.is_file():
                        raise ValueError(
                            f"Piper voice configuration is missing: {piper_config.name}"
                        )
                elevenlabs_voice_id = self.setting_elevenlabs_voice_id.text().strip() or None
                if speech_enabled and tts_provider == "elevenlabs":
                    if elevenlabs_voice_id is None:
                        raise ValueError("Enter an ElevenLabs voice ID")
                    if not (
                        self.setting_elevenlabs_key.text().strip()
                        or container.secret_vault.has("elevenlabs")
                    ):
                        raise ValueError("Enter an ElevenLabs API key")
                wake_word_text = self.setting_wake_word_model.text().strip()
                wake_word_model = Path(wake_word_text).expanduser() if wake_word_text else None
                if self.setting_wake_word_enabled.isChecked():
                    if wake_word_model is None or not wake_word_model.is_file():
                        raise ValueError("Select an existing wake-word ONNX model file")
                    if wake_word_model.suffix.casefold() != ".onnx":
                        raise ValueError("Wake-word model must be an .onnx file")
                confirmation_overrides = {
                    key: checkbox.isChecked()
                    for key, default, checkbox in self.setting_confirmation_prompt_rows
                    if checkbox.isChecked() != default
                }
                values = {
                    "offline_lock": self.setting_offline.isChecked(),
                    "scheduler_enabled": self.setting_scheduler_enabled.isChecked(),
                    "background_actions_enabled": self.setting_background_actions.isChecked(),
                    "pause_background_while_locked": self.setting_pause_while_locked.isChecked(),
                    "notify_on_background_success": (
                        self.setting_notify_background_success.isChecked()
                    ),
                    "notify_on_background_failure": (
                        self.setting_notify_background_failure.isChecked()
                    ),
                    "show_in_app_notifications": (
                        self.setting_show_in_app_notifications.isChecked()
                    ),
                    "show_system_notifications": (
                        self.setting_show_system_notifications.isChecked()
                    ),
                    "notification_quiet_hours_enabled": (
                        self.setting_quiet_hours_enabled.isChecked()
                    ),
                    "notification_quiet_hours_start": (
                        self.setting_quiet_hours_start.text().strip()
                    ),
                    "notification_quiet_hours_end": (self.setting_quiet_hours_end.text().strip()),
                    "default_timezone": self.setting_default_timezone.text().strip(),
                    "audio_input_device": self.setting_input.currentData(),
                    "stt_model": self.setting_stt.text().strip(),
                    "llm_model": self.setting_llm.text().strip(),
                    "llm_keep_alive": self.setting_llm_keep_alive.text().strip(),
                    "llm_preload": self.setting_llm_preload.isChecked(),
                    "openai_model": self.setting_openai_model.text().strip(),
                    "openai_codex_model": (self.setting_openai_codex_model.text().strip()),
                    "anthropic_model": self.setting_anthropic_model.text().strip(),
                    "cloud_pricing": cloud_pricing,
                    "search_provider": self.setting_search_provider.currentData(),
                    "extension_catalog_url": (
                        self.setting_extension_catalog_url.text().strip() or None
                    ),
                    "tts_provider": tts_provider,
                    "piper_executable": piper_executable,
                    "piper_model": piper_model,
                    "elevenlabs_voice_id": elevenlabs_voice_id,
                    "elevenlabs_model_id": (self.setting_elevenlabs_model.currentText().strip()),
                    "speech_mode": self.setting_speech.currentText(),
                    "voice_activation_mode": (self.setting_voice_activation_mode.currentData()),
                    "wake_word_enabled": self.setting_wake_word_enabled.isChecked(),
                    "wake_word_model": wake_word_model,
                    "wake_word_threshold": float(self.setting_wake_word_threshold.text().strip()),
                    "voice_activation_keyword_enabled": (
                        self.setting_voice_keyword_enabled.isChecked()
                    ),
                    "voice_activation_keyword": (self.setting_voice_keyword.text().strip()),
                    "voice_activation_clap_enabled": (self.setting_voice_clap_enabled.isChecked()),
                    "voice_activation_clap_threshold": float(
                        self.setting_voice_clap_threshold.text().strip()
                    ),
                    "push_to_talk_hotkey": self.setting_ptt.text().strip(),
                    "cancel_hotkey": self.setting_cancel.text().strip(),
                    "retain_transcripts_days": int(self.setting_retention.text().strip()),
                    "dashboard_history_retention_days": int(
                        self.setting_dashboard_history_retention.text().strip()
                    ),
                    "diagnostic_audio": self.setting_diagnostic_audio.isChecked(),
                    "noise_suppression": self.setting_noise.isChecked(),
                    "automatic_gain_control": self.setting_gain.isChecked(),
                    "response_detail": self.setting_detail.currentText(),
                    "ui_text_scale": self.setting_text_scale.currentData(),
                    "ui_high_contrast": self.setting_high_contrast.isChecked(),
                    "ui_reduced_motion": self.setting_reduced_motion.isChecked(),
                    "show_reasoning_steps": self.setting_show_reasoning.isChecked(),
                    "terminal_debug": self.setting_terminal_debug.isChecked(),
                    "hide_sensitive_display": self.setting_hide_sensitive.isChecked(),
                    "confirmation_overrides": confirmation_overrides,
                }
                validated = container.settings.model_copy(update=values)
                validated = type(container.settings).model_validate(validated.model_dump())
            except Exception as error:
                QMessageBox.critical(self, "Invalid Settings", str(error))
                return
            self._persist_application_settings(validated)
            container.extension_hub.configure(
                validated.extension_catalog_url,
                timeout_seconds=validated.extension_catalog_timeout_seconds,
            )
            self.extension_hub_page.set_configuration(
                validated.extension_catalog_url,
                offline=self._startup_offline_lock or validated.offline_lock,
            )
            container.scheduler.enabled = validated.scheduler_enabled
            if validated.scheduler_enabled and not container.background_runtime.running:
                container.background_runtime.start()
            container.scheduler.runner.background_actions_enabled = (
                validated.background_actions_enabled
            )
            container.scheduler.runner.pause_while_locked = validated.pause_background_while_locked
            container.scheduler.runner.notify_on_success = validated.notify_on_background_success
            container.scheduler.runner.notify_on_failure = validated.notify_on_background_failure
            container.scheduler.max_concurrency = validated.max_background_concurrency
            container.scheduler.wake()
            vault = SecretVault(validated.data_dir / "secrets")
            for name, field in (
                ("brave", self.setting_brave_key),
                ("elevenlabs", self.setting_elevenlabs_key),
            ):
                if field.text().strip():
                    vault.set(name, field.text())
                    field.clear()
                    field.setPlaceholderText("Stored; enter a new key to replace")
            if isinstance(container.orchestrator.tts, ElevenLabsProvider):
                container.orchestrator.tts.set_offline_lock(
                    validated.offline_lock or validated.tts_provider != "elevenlabs"
                )
            container.media_cache.set_offline_lock(validated.offline_lock)
            container.orchestrator.speech_mode = validated.speech_mode
            container.orchestrator.response_detail = validated.response_detail
            if container.orchestrator.planner is not None:
                container.orchestrator.planner.response_detail = validated.response_detail
                planner_provider = getattr(
                    container.orchestrator.planner,
                    "provider",
                    None,
                )
                if planner_provider is not None and hasattr(planner_provider, "keep_alive"):
                    planner_provider.keep_alive = validated.llm_keep_alive
                    if validated.llm_preload and hasattr(
                        planner_provider,
                        "preload_in_background",
                    ):
                        planner_provider.preload_in_background(
                            timeout_seconds=min(
                                15.0,
                                validated.planner_timeout_seconds,
                            )
                        )
            container.orchestrator.show_reasoning_steps = validated.show_reasoning_steps
            container.orchestrator.terminal_debug = validated.terminal_debug
            container.orchestrator.hide_sensitive_display = validated.hide_sensitive_display
            self._apply_accessibility_settings(validated)
            container.engine.confirmation_overrides = dict(validated.confirmation_overrides)
            container.audio_capture.noise_suppression = validated.noise_suppression
            container.audio_capture.automatic_gain_control = validated.automatic_gain_control
            container.audio_capture.clap_detector.threshold = (
                validated.voice_activation_clap_threshold
            )
            compact_mode_index = self.compact_panel.activation_mode.findData(
                validated.voice_activation_mode
            )
            self.compact_panel.activation_mode.blockSignals(True)
            self.compact_panel.activation_mode.setCurrentIndex(max(0, compact_mode_index))
            self.compact_panel.activation_mode.blockSignals(False)
            self._start_passive_voice()
            QMessageBox.information(
                self,
                "Settings Saved",
                "Settings were saved. Speech provider, model, wake-word, device, and global "
                "hotkey changes take full effect after restart.",
            )

        def _diagnostics_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.diagnostic_text = QTextEdit()
            self.diagnostic_text.setReadOnly(True)
            self._refresh_diagnostics()
            controls = QHBoxLayout()
            refresh = QPushButton("Refresh Diagnostics")
            refresh.clicked.connect(self._refresh_diagnostics)
            benchmark = QPushButton("Benchmark Deterministic Command")
            benchmark.clicked.connect(self._benchmark)
            setup = QPushButton("Resume Guided Setup")
            setup.setAccessibleName("Resume guided setup")
            setup.clicked.connect(lambda: self._first_run(force=True))
            controls.addWidget(refresh)
            controls.addWidget(benchmark)
            controls.addWidget(setup)
            controls.addStretch()
            layout.addWidget(self.diagnostic_text)
            layout.addLayout(controls)
            return page

        def _refresh_diagnostics(self) -> None:
            report = hardware_report(
                media_cache=container.media_cache,
                media_sessions=container.media_sessions,
            )
            recommendations = model_recommendations(report)
            recent_metrics = container.metrics.recent(100)
            dashboard = container.dashboard.diagnostics()
            command_latencies = [
                item["duration_ms"]
                for item in recent_metrics
                if item["stage"] == "execution" and item["duration_ms"] is not None
            ]
            multimedia = report["multimedia"]
            issues = recovery_issues(
                report,
                database_integrity=container.database.integrity_check(),
                requested_stt_device=container.settings.stt_device,
            )
            self.diagnostic_text.setPlainText(
                "\n".join(
                    [
                        f"OS: {report['os']}",
                        f"Python: {report['python']}",
                        f"Processor: {report['processor']}",
                        f"GPU: {report['gpu'] or 'Not detected'}",
                        f"Audio endpoints: {report['audio_devices']}",
                        "",
                        "In-app media",
                        "Backend: " + ("Available" if multimedia["available"] else "Unavailable"),
                        "Image formats: "
                        + (", ".join(multimedia["image_formats"]) or "None detected"),
                        "Audio/video formats: "
                        + (", ".join(multimedia["audio_video_formats"]) or "None detected"),
                        "Output devices: "
                        + (", ".join(multimedia["output_devices"]) or "None detected"),
                        f"Downloaded cache: {multimedia['cache_bytes'] / (1024 * 1024):.1f} MB",
                        "Last decoder error: " + (multimedia["last_error_code"] or "None"),
                        "",
                        f"Recommended STT: {recommendations['stt']}",
                        f"Recommended LLM: {recommendations['llm']}",
                        f"Recommended TTS: {recommendations['tts']}",
                        f"Database integrity: {container.database.integrity_check()}",
                        "Recent execution average: "
                        + (
                            f"{sum(command_latencies) / len(command_latencies):.1f} ms"
                            if command_latencies
                            else "No samples"
                        ),
                        "",
                        "Dashboard",
                        f"Cards: {dashboard.get('cards', 0)}",
                        f"Connections: {dashboard.get('connections', 0)}",
                        f"Refresh attempts: {dashboard.get('refresh_started', 0)}",
                        f"Ready snapshots: {dashboard.get('ready', 0)}",
                        f"Stale fallbacks: {dashboard.get('stale', 0)}",
                        f"Permission blocks: {dashboard.get('permission_required', 0)}",
                        f"Offline blocks: {dashboard.get('offline', 0)}",
                        f"Timeouts: {dashboard.get('timeout', 0)}",
                        f"Refresh errors: {dashboard.get('error', 0)}",
                        f"History errors: {dashboard.get('history_error', 0)}",
                        f"Numeric history samples: {dashboard.get('history_samples', 0)}",
                        "",
                        "Recovery",
                        *(
                            [
                                f"[{issue.severity.upper()}] {issue.title}: {issue.action}"
                                for issue in issues
                            ]
                            or ["No known recovery actions are required."]
                        ),
                    ]
                )
            )

        def _macros_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.macro_list = QListWidget()
            self._reload_macros()
            controls = QHBoxLayout()
            add = QPushButton("Add or Update Macro")
            add.clicked.connect(self._edit_macro)
            delete = QPushButton("Delete Selected Macro")
            delete.clicked.connect(self._delete_macro)
            controls.addWidget(add)
            controls.addWidget(delete)
            layout.addWidget(
                self._info_label(
                    "Macro steps use registered skill IDs and JSON arguments. "
                    "Every step keeps its own permissions and validation."
                )
            )
            layout.addWidget(self.macro_list)
            layout.addLayout(controls)
            return page

        def _reload_macros(self) -> None:
            self.macro_list.clear()
            for definition in container.macro_store.list():
                self.macro_list.addItem(
                    f"{definition.name}: {len(definition.steps)} step(s) - {definition.description}"
                )

        def _edit_macro(self) -> None:
            name, ok = QInputDialog.getText(self, "Macro", "Macro name")
            if not ok or not name.strip():
                return
            description, ok = QInputDialog.getText(self, "Macro", "Description")
            if not ok:
                return
            example = '[{"skill_id":"core.calculator","arguments":{"expression":"2 + 2"}}]'
            raw, ok = QInputDialog.getMultiLineText(
                self, "Macro Steps", "JSON list of steps", example
            )
            if not ok:
                return
            try:
                values = json.loads(raw)
                steps = tuple(
                    MacroStep(
                        skill_id=item["skill_id"],
                        arguments=item.get("arguments", {}),
                    )
                    for item in values
                )
                for step in steps:
                    if step.skill_id == "core.macro":
                        raise ValueError("Macros cannot call macros")
                    container.registry.get(step.skill_id).validate(step.arguments)
                container.macro_store.save(MacroDefinition(name, description, steps))
            except Exception as error:
                QMessageBox.critical(self, "Invalid Macro", str(error))
                return
            self._reload_macros()

        def _delete_macro(self) -> None:
            row = self.macro_list.currentRow()
            definitions = container.macro_store.list()
            if row < 0 or row >= len(definitions):
                return
            container.macro_store.delete(definitions[row].name)
            self._reload_macros()

        def _playbooks_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(
                self._info_label(
                    "Playbooks are user-authored Markdown planning instructions. "
                    "Their skill IDs ground the planner, but they never grant permissions "
                    "or bypass confirmations. Files are stored in "
                    f"{container.playbook_store.root}."
                )
            )
            self.playbook_list = QListWidget()
            self.playbook_list.setAccessibleName("User playbooks")
            self.playbook_errors = QLabel("")
            self.playbook_errors.setWordWrap(True)
            self.playbook_errors.setObjectName("activityHint")
            controls = QHBoxLayout()
            import_button = QPushButton("Import Markdown")
            import_button.clicked.connect(self._import_playbook)
            add_button = QPushButton("New from Template")
            add_button.clicked.connect(self._add_playbook)
            edit_button = QPushButton("Edit Selected")
            edit_button.clicked.connect(self._edit_playbook)
            toggle_button = QPushButton("Enable / Disable")
            toggle_button.clicked.connect(self._toggle_playbook)
            delete_button = QPushButton("Delete Selected")
            delete_button.clicked.connect(self._delete_playbook)
            for button in (
                import_button,
                add_button,
                edit_button,
                toggle_button,
                delete_button,
            ):
                controls.addWidget(button)
            layout.addWidget(self.playbook_list)
            layout.addWidget(self.playbook_errors)
            layout.addLayout(controls)
            self._reload_playbooks()
            return page

        def _reload_playbooks(self) -> None:
            self.playbook_list.clear()
            enabled_skill_ids = {
                manifest.skill_id
                for manifest in container.registry.list()
                if container.registry.is_enabled(manifest.skill_id)
            }
            for playbook in container.playbook_store.list():
                state = "Enabled" if playbook.enabled else "Disabled"
                missing = tuple(
                    skill_id for skill_id in playbook.skill_ids if skill_id not in enabled_skill_ids
                )
                suffix = f" | Unavailable: {', '.join(missing)}" if missing else ""
                item = QListWidgetItem(
                    f"{state} | {playbook.name} | {len(playbook.skills)} skill(s){suffix}"
                )
                item.setData(Qt.ItemDataRole.UserRole, playbook.playbook_id)
                item.setToolTip(playbook.description or playbook.playbook_id)
                self.playbook_list.addItem(item)
            errors = container.playbook_store.errors()
            if errors:
                details = "; ".join(
                    f"{Path(path).name}: {message}" for path, message in sorted(errors.items())
                )
                self.playbook_errors.setText(f"Invalid files ignored: {details}")
                self.playbook_errors.show()
            else:
                self.playbook_errors.clear()
                self.playbook_errors.hide()

        def _selected_playbook(self):
            item = self.playbook_list.currentItem()
            if item is None:
                return None
            playbook_id = item.data(Qt.ItemDataRole.UserRole)
            try:
                return container.playbook_store.get(str(playbook_id))
            except KeyError:
                return None

        def _import_playbook(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Import Playbook",
                "",
                "Markdown playbooks (*.md *.markdown)",
            )
            if not path:
                return
            try:
                playbook = container.playbook_store.import_file(Path(path))
            except Exception as error:
                QMessageBox.critical(self, "Playbook Import Failed", str(error))
                return
            self._warn_for_unavailable_playbook_skills(playbook)
            self._reload_playbooks()

        def _add_playbook(self) -> None:
            self._edit_playbook_markdown(None, container.playbook_store.EXAMPLE)

        def _edit_playbook(self) -> None:
            selected = self._selected_playbook()
            if selected is None or selected.source_path is None:
                return
            try:
                markdown = selected.source_path.read_text(encoding="utf-8-sig")
            except Exception as error:
                QMessageBox.critical(self, "Playbook Read Failed", str(error))
                return
            self._edit_playbook_markdown(selected, markdown)

        def _edit_playbook_markdown(self, selected, markdown: str) -> None:
            value, ok = QInputDialog.getMultiLineText(
                self,
                "Playbook Markdown",
                "YAML frontmatter followed by workflow instructions",
                markdown,
            )
            if not ok:
                return
            try:
                playbook = container.playbook_store.save_markdown(
                    value,
                    original_id=selected.playbook_id if selected else None,
                )
            except Exception as error:
                QMessageBox.critical(self, "Playbook Save Failed", str(error))
                return
            self._warn_for_unavailable_playbook_skills(playbook)
            self._reload_playbooks()

        def _warn_for_unavailable_playbook_skills(self, playbook) -> None:
            enabled_skill_ids = {
                manifest.skill_id
                for manifest in container.registry.list()
                if container.registry.is_enabled(manifest.skill_id)
            }
            missing = tuple(
                skill_id for skill_id in playbook.skill_ids if skill_id not in enabled_skill_ids
            )
            if missing:
                QMessageBox.warning(
                    self,
                    "Unavailable Playbook Skills",
                    "The playbook was saved, but these skill IDs are unavailable and will "
                    "not be selected: " + ", ".join(missing),
                )

        def _toggle_playbook(self) -> None:
            selected = self._selected_playbook()
            if selected is None:
                return
            try:
                container.playbook_store.set_enabled(
                    selected.playbook_id,
                    not selected.enabled,
                )
            except Exception as error:
                QMessageBox.critical(self, "Playbook Update Failed", str(error))
                return
            self._reload_playbooks()

        def _delete_playbook(self) -> None:
            selected = self._selected_playbook()
            if selected is None or not self._confirm(f'Delete playbook "{selected.name}"?'):
                return
            container.playbook_store.delete(selected.playbook_id)
            self._reload_playbooks()

        def _models_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.model_list = QListWidget()
            self._reload_models()
            install = QPushButton("Install Signed Model Package")
            install.clicked.connect(self._install_model)
            select = QPushButton("Select Existing Model Location")
            select.clicked.connect(self._select_model_location)
            layout.addWidget(
                QLabel(
                    "Models install from local media by default. Online downloads "
                    "require explicit network approval."
                )
            )
            layout.addWidget(self.model_list)
            layout.addWidget(install)
            layout.addWidget(select)
            return page

        def _knowledge_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(
                QLabel(
                    "Store local reference notes and UTF-8 text files. Enabled "
                    "knowledge is retrieved for relevant conversational questions."
                )
            )
            self.knowledge_list = QListWidget()
            self._reload_knowledge()
            controls = QHBoxLayout()
            import_button = QPushButton("Import File")
            import_button.clicked.connect(self._import_knowledge)
            add_button = QPushButton("Add Note")
            add_button.clicked.connect(self._add_knowledge)
            edit_button = QPushButton("Edit Selected")
            edit_button.clicked.connect(self._edit_knowledge)
            toggle_button = QPushButton("Enable / Disable")
            toggle_button.clicked.connect(self._toggle_knowledge)
            delete_button = QPushButton("Delete Selected")
            delete_button.clicked.connect(self._delete_knowledge)
            for button in (
                import_button,
                add_button,
                edit_button,
                toggle_button,
                delete_button,
            ):
                controls.addWidget(button)
            layout.addWidget(self.knowledge_list)
            layout.addLayout(controls)
            return page

        def _reload_knowledge(self) -> None:
            self.knowledge_list.clear()
            for item in container.knowledge_bases.list():
                source = Path(item.source_path).name if item.source_path else "note"
                state = "Enabled" if item.enabled else "Disabled"
                self.knowledge_list.addItem(
                    f"{state} | {item.name} | {source} | {len(item.content):,} chars"
                )

        def _selected_knowledge(self):
            row = self.knowledge_list.currentRow()
            values = container.knowledge_bases.list()
            return values[row] if 0 <= row < len(values) else None

        def _import_knowledge(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Import Knowledge File",
                "",
                "Knowledge files (*.txt *.md *.markdown *.rst *.csv *.json)",
            )
            if not path:
                return
            try:
                container.knowledge_bases.import_file(Path(path))
            except Exception as error:
                QMessageBox.critical(self, "Knowledge Import Failed", str(error))
                return
            self._reload_knowledge()

        def _add_knowledge(self) -> None:
            self._edit_knowledge_value(None)

        def _edit_knowledge(self) -> None:
            selected = self._selected_knowledge()
            if selected is not None:
                self._edit_knowledge_value(selected)

        def _edit_knowledge_value(self, selected) -> None:
            name, ok = QInputDialog.getText(
                self,
                "Knowledge Base",
                "Name",
                text=selected.name if selected else "",
            )
            if not ok or not name.strip():
                return
            description, ok = QInputDialog.getText(
                self,
                "Knowledge Base",
                "Description (helps retrieval)",
                text=selected.description if selected else "",
            )
            if not ok:
                return
            content, ok = QInputDialog.getMultiLineText(
                self,
                "Knowledge Base Content",
                "Reference information",
                selected.content if selected else "",
            )
            if not ok:
                return
            try:
                container.knowledge_bases.save(
                    name,
                    content,
                    description=description,
                    source_path=selected.source_path if selected else None,
                    enabled=selected.enabled if selected else True,
                    knowledge_base_id=selected.id if selected else None,
                )
            except Exception as error:
                QMessageBox.critical(self, "Knowledge Save Failed", str(error))
                return
            self._reload_knowledge()

        def _toggle_knowledge(self) -> None:
            selected = self._selected_knowledge()
            if selected is None:
                return
            container.knowledge_bases.set_enabled(selected.id, not selected.enabled)
            self._reload_knowledge()

        def _delete_knowledge(self) -> None:
            selected = self._selected_knowledge()
            if selected is None or not self._confirm(f'Delete knowledge base "{selected.name}"?'):
                return
            container.knowledge_bases.delete(selected.id)
            self._reload_knowledge()

        def _memories_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(
                QLabel(
                    "Stored memories are explicit user-approved facts and preferences. "
                    "Enabled memories can be used as assistant context; disabled "
                    "memories remain stored until deleted."
                )
            )
            self.memory_table = QTableWidget(0, 6)
            self.memory_table.setHorizontalHeaderLabels(
                ["Status", "Type", "Key", "Memory", "Source", "Confidence"]
            )
            self._configure_data_table(self.memory_table)
            self._reload_memories()
            controls = QHBoxLayout()
            refresh = QPushButton("Refresh")
            refresh.clicked.connect(self._reload_memories)
            edit = QPushButton("Edit Selected")
            edit.clicked.connect(self._edit_memory)
            toggle = QPushButton("Enable / Disable")
            toggle.clicked.connect(self._toggle_memory)
            delete = QPushButton("Delete Selected")
            delete.setObjectName("dangerButton")
            delete.clicked.connect(self._delete_memory)
            for button in (refresh, edit, toggle, delete):
                controls.addWidget(button)
            layout.addWidget(self.memory_table)
            layout.addLayout(controls)
            return page

        def _reload_memories(self) -> None:
            self.memory_table.setRowCount(0)
            for item in container.memory_store.list(include_disabled=True):
                row = self.memory_table.rowCount()
                self.memory_table.insertRow(row)
                fields = [
                    "Enabled" if item.enabled else "Disabled",
                    item.type,
                    item.key,
                    item.value,
                    item.source,
                    f"{item.confidence:.2f}",
                ]
                for column, field in enumerate(fields):
                    table_item = self._table_item(field)
                    if column == 0:
                        table_item.setData(Qt.ItemDataRole.UserRole, item.id)
                    self.memory_table.setItem(row, column, table_item)

        def _selected_memory(self):
            row = self.memory_table.currentRow()
            if row < 0:
                return None
            id_item = self.memory_table.item(row, 0)
            if id_item is None:
                return None
            return container.memory_store.get(id_item.data(Qt.ItemDataRole.UserRole))

        def _edit_memory(self) -> None:
            selected = self._selected_memory()
            if selected is None:
                return
            value, ok = QInputDialog.getMultiLineText(
                self,
                "Edit Memory",
                "Memory",
                selected.value,
            )
            if not ok:
                return
            try:
                container.memory_store.update(
                    memory_id=selected.id,
                    value=value,
                )
            except Exception as error:
                QMessageBox.critical(self, "Memory Save Failed", str(error))
                return
            self._reload_memories()

        def _toggle_memory(self) -> None:
            selected = self._selected_memory()
            if selected is None:
                return
            container.memory_store.update(
                memory_id=selected.id,
                enabled=not selected.enabled,
            )
            self._reload_memories()

        def _delete_memory(self) -> None:
            selected = self._selected_memory()
            if selected is None or not self._confirm(f'Delete memory "{selected.key}"?'):
                return
            container.memory_store.delete(memory_id=selected.id)
            self._reload_memories()

        def _reload_memories_if_visible(self) -> None:
            if hasattr(self, "memory_table"):
                self._reload_memories()

        def _privacy_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(
                QLabel(
                    "Clear each local data category independently. Secrets are "
                    "never included in configuration exports."
                )
            )
            actions = [
                ("Clear Transcript History", self._clear_history),
                ("Clear Audit Log", self._clear_audit),
                ("Clear Knowledge Bases", self._clear_knowledge),
                ("Clear Playbooks", self._clear_playbooks),
                ("Clear Stored Memories", self._clear_memories),
                ("Clear TTS Cache", lambda: self._clear_directory("tts-cache")),
                ("Clear Downloaded Media Cache", self._clear_media_cache),
                (
                    "Clear Diagnostic Audio",
                    lambda: self._clear_directory("diagnostic-audio"),
                ),
                ("Clear Downloaded Models", lambda: self._clear_directory("models")),
                ("Export Non-Secret Configuration", self._export_configuration),
                ("Back Up Database", self._backup_database),
            ]
            for label, callback in actions:
                button = QPushButton(label)
                button.clicked.connect(callback)
                layout.addWidget(button)
            layout.addStretch()
            return page

        def _clear_knowledge(self) -> None:
            if self._confirm("Delete all local knowledge bases?"):
                container.knowledge_bases.clear()
                self._reload_knowledge()

        def _clear_playbooks(self) -> None:
            if self._confirm("Delete all local playbook Markdown files?"):
                container.playbook_store.clear()
                self._reload_playbooks()

        def _clear_memories(self) -> None:
            if not self._confirm("Delete all stored memories, including disabled ones?"):
                return
            for item in container.memory_store.list(include_disabled=True):
                container.memory_store.delete(memory_id=item.id)
            self._reload_memories_if_visible()

        def _clear_media_cache(self) -> None:
            if not self._confirm("Close any remote media viewer and delete all downloaded media?"):
                return
            snapshot = container.media_sessions.current()
            if snapshot is not None and snapshot.source.source_type.value == "remote_cache":
                self.media_viewer.clear()
            removed = container.media_cache.clear()
            self.statusBar().showMessage(
                f"Cleared {removed / (1024 * 1024):.1f} MB of downloaded media.",
                5000,
            )

        def _applications_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.application_list = QListWidget()
            self._reload_applications()
            controls = QHBoxLayout()
            add = QPushButton("Register Application")
            add.clicked.connect(self._register_application)
            self.bulk_register_applications_button = QPushButton("Register Installed Apps")
            self.bulk_register_applications_button.clicked.connect(
                self._register_installed_applications
            )
            delete = QPushButton("Remove Selected")
            delete.clicked.connect(self._remove_application)
            controls.addWidget(add)
            controls.addWidget(self.bulk_register_applications_button)
            controls.addWidget(delete)
            layout.addWidget(QLabel("Only applications registered here can be launched by skills."))
            self.application_status = QLabel("")
            self.application_status.setObjectName("pageSubtitle")
            layout.addWidget(self.application_list)
            layout.addWidget(self.application_status)
            layout.addLayout(controls)
            return page

        def _reload_applications(self) -> None:
            self.application_list.clear()
            for name, path in container.aliases.all("application").items():
                self.application_list.addItem(f"{name} | {path}")

        def _register_application(self) -> None:
            self._register_application_alias()

        def _register_installed_applications(self) -> None:
            if (
                QMessageBox.question(
                    self,
                    "Register Installed Apps",
                    "Scan installed applications and register every launchable executable found?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            self.bulk_register_applications_button.setEnabled(False)
            self.application_status.setText("Scanning installed applications...")

            async def scan_apps():
                return await LocalWindowsDeviceAdapter().search_installed_apps(limit=500)

            worker = AsyncWorker(scan_apps())
            worker.complete.connect(self._finish_register_installed_applications)
            worker.failed.connect(self._fail_register_installed_applications)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _finish_register_installed_applications(self, apps: list[dict]) -> None:
            aliases = {
                str(app["name"]): str(Path(app["executable_path"]).resolve())
                for app in apps
                if app.get("name")
                and app.get("executable_path")
                and Path(str(app["executable_path"])).is_file()
            }
            added, updated = container.aliases.set_many("application", aliases)
            skipped = len(apps) - len(aliases)
            self._reload_applications()
            self.bulk_register_applications_button.setEnabled(True)
            self.application_status.setText(
                f"Registered {added} new and updated {updated}; skipped {skipped} without launchable executables."
            )
            QMessageBox.information(
                self,
                "Installed Apps Registered",
                self.application_status.text(),
            )

        def _fail_register_installed_applications(self, error: str) -> None:
            self.bulk_register_applications_button.setEnabled(True)
            self.application_status.setText("Installed application scan failed.")
            self._show_error(error)

        def _register_application_alias(self, requested_name: str | None = None) -> bool:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select Application", "", "Applications (*.exe)"
            )
            if not path:
                return False
            name, ok = QInputDialog.getText(
                self,
                "Application Name",
                "Spoken/display name",
                text=requested_name or Path(path).stem,
            )
            if ok and name.strip():
                container.aliases.set("application", name, str(Path(path).resolve()))
                self._reload_applications()
                return True
            return False

        def _remove_application(self) -> None:
            row = self.application_list.currentRow()
            values = list(container.aliases.all("application"))
            if 0 <= row < len(values):
                container.aliases.delete("application", values[row])
                self._reload_applications()

        def _confirm(self, message: str) -> bool:
            return (
                QMessageBox.question(
                    self,
                    "Confirm Local Data Change",
                    message,
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                == QMessageBox.Yes
            )

        def _clear_history(self) -> None:
            if self._confirm("Delete all locally stored command history?"):
                HistoryStore(container.database).clear()

        def _clear_audit(self) -> None:
            if self._confirm("Delete the complete local security audit log?"):
                container.audit.clear()

        def _clear_directory(self, name: str) -> None:
            if not self._confirm(f"Delete all local {name} data?"):
                return
            root = container.settings.data_dir.resolve()
            target = (root / name).resolve()
            if root not in target.parents:
                QMessageBox.critical(self, "Unsafe Path", str(target))
                return
            shutil.rmtree(target, ignore_errors=True)
            if name == "models":
                self._reload_models()

        def _export_configuration(self) -> None:
            destination, _ = QFileDialog.getSaveFileName(
                self, "Export Configuration", "assistant-settings.json", "JSON (*.json)"
            )
            if not destination:
                return
            payload = container.settings.model_dump(
                mode="json",
                exclude={"trusted_model_keys", "trusted_skill_keys"},
            )
            Path(destination).write_text(json.dumps(payload, indent=2), encoding="utf-8")

        def _backup_database(self) -> None:
            destination, _ = QFileDialog.getSaveFileName(
                self, "Back Up Database", "assistant-backup.db", "Database (*.db)"
            )
            if destination:
                container.database.backup(Path(destination))

        def _reload_models(self) -> None:
            self.model_list.clear()
            for value in container.model_manager.installed():
                self.model_list.addItem(
                    f"{value['component']} | {value['model_id']} | {value['path']}"
                )

        def _install_model(self) -> None:
            manifest_path, _ = QFileDialog.getOpenFileName(
                self, "Select Model Manifest", "", "JSON (*.json)"
            )
            if not manifest_path:
                return
            model_path, _ = QFileDialog.getOpenFileName(
                self, "Select Model File", str(Path(manifest_path).parent)
            )
            if not model_path:
                return
            try:
                manifest = ModelManifest.model_validate_json(Path(manifest_path).read_text("utf-8"))
                destination = container.model_manager.install_from_file(Path(model_path), manifest)
            except Exception as error:
                QMessageBox.critical(self, "Model Installation Failed", str(error))
                return
            QMessageBox.information(self, "Model Installed", f"Installed to {destination}")
            self._reload_models()

        def _select_model_location(self) -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Select Existing Local Model")
            if not path:
                return
            component, ok = QInputDialog.getItem(
                self,
                "Model Component",
                "Use this model for",
                ["stt", "llm", "tts", "wakeword"],
                editable=False,
            )
            if ok:
                SettingsStore(container.database).set(f"model_location.{component}", path)
                QMessageBox.information(self, "Model Location Saved", f"{component}: {path}")

        def _first_run(self, *, force: bool = False) -> None:
            settings_store = SettingsStore(container.database)
            onboarding_store = OnboardingStore(settings_store)
            if settings_store.get("first_run_complete", False) and not force:
                return
            if force:
                onboarding_store.reset()
            report = hardware_report()
            recommendations = model_recommendations(report)

            def open_tab(label: str) -> None:
                self._switch_window_mode("full")
                self.tabs.setCurrentIndex(TAB_LABELS.index(label))

            self._onboarding_dialog = OnboardingDialog(
                onboarding_store,
                hardware=report,
                recommendations=recommendations,
                actions={
                    SetupStep.MODELS: lambda: open_tab("Models"),
                    SetupStep.AUDIO: lambda: open_tab("Settings"),
                    SetupStep.ACTIVATION: lambda: open_tab("Settings"),
                    SetupStep.PERMISSIONS: lambda: open_tab("Permissions"),
                    SetupStep.TYPED_TEST: lambda: self._run_text_command("calculate 17 times 19"),
                    SetupStep.VOICE_TEST: self.listen,
                    SetupStep.SAFETY: self.cancel,
                    SetupStep.FINISH: lambda: open_tab("Diagnostics"),
                },
                parent=self,
            )
            self._onboarding_dialog.open()

        def _benchmark(self) -> None:
            async def run():
                started = time.perf_counter()
                await container.orchestrator.handle_text("calculate 17 times 19")
                return (time.perf_counter() - started) * 1000

            worker = AsyncWorker(run())
            worker.complete.connect(
                lambda milliseconds: self.diagnostic_text.append(
                    f"Deterministic command latency: {milliseconds:.1f} ms"
                )
            )
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def run_command(self) -> None:
            text = self.transcript.toPlainText().strip()
            if not text:
                self.transcript.setFocus()
                return
            self._run_text_command(text)

        def _run_compact_command(self, text: str) -> None:
            if self._run_text_command(text):
                self.compact_panel.composer.clear()

        def _run_text_command(self, text: str) -> bool:
            text = text.strip()
            if not text or self._pipeline_is_busy() or self._voice_prompt_active:
                return False
            self.transcript.setPlainText(text)
            self.compact_panel.set_request(text)
            self._activity_started = time.perf_counter()
            self._append_activity("Request", "Started a new interactive request")
            self._set_pipeline_state(PipelineState.THINKING, "Interpreting your request")
            worker = AsyncWorker(controller.run_text(text))
            worker.complete.connect(self._show_result)
            worker.cancelled.connect(self._show_cancelled)
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()
            return True

        def _show_result(self, payload: Any) -> None:
            response, outcome = payload
            if getattr(self, "_last_event_response", None) != response:
                self._display_response(response)
            self._last_event_response = None
            if outcome.call is not None and outcome.call.skill_id == "core.memory":
                self._reload_memories_if_visible()
            if outcome.registration_requests:
                self._request_registrations(outcome)
            elif outcome.permission_requests:
                self._request_permissions(outcome)
            elif outcome.confirmation:
                self._request_confirmation(outcome)
            else:
                self._set_pipeline_state(
                    PipelineState.IDLE,
                    self._passive_voice_status()
                    if self._passive_voice_worker and self._passive_voice_worker.isRunning()
                    else "Request complete",
                )
                self._apply_passive_voice_indicators()

        def _show_voice_result(self, payload: Any) -> None:
            response, _outcome = payload
            if response.updates_display and self._pending_voice_transcript:
                self._commit_voice_transcript(self._pending_voice_transcript)
            elif not response.updates_display:
                self._pending_voice_transcript = None
            self._show_result(payload)

        def _request_permissions(
            self,
            outcome: Any,
            *,
            continuation: Any | None = None,
            cancelled: Any | None = None,
            resume: Any | None = None,
        ) -> None:
            details = []
            for request in outcome.permission_requests:
                manifest = container.registry.get(request.call.skill_id).manifest
                details.append(
                    f"{manifest.display_name}\n"
                    f"{capability_name(request.scope.capability)}: "
                    f"{capability_description(request.scope.capability)}\n"
                    f"Why: {request.scope.reason}\n"
                    f"Targets: {target_summary(request.scope.targets)}"
                )
            decision = self._voice_prompt_choice(
                title="Permission Required",
                text="This command needs permission to continue.",
                informative_text="\n\n".join(details),
                kind="approval",
                buttons=[
                    (
                        "approve",
                        "Grant Permission",
                        QMessageBox.ButtonRole.AcceptRole,
                    ),
                    ("deny", "Not Now", QMessageBox.ButtonRole.RejectRole),
                ],
                default_choice="deny",
            )
            if decision != "approve":
                if continuation is None:
                    controller.discard_replanning_continuation(outcome)
                self._set_pipeline_state(PipelineState.CANCELLED, "Permission was not granted")
                if cancelled is not None:
                    cancelled()
                return
            labels = {
                "once": GrantType.ONCE,
                "session": GrantType.SESSION,
                "always": GrantType.ALWAYS,
            }
            mode_label = self._voice_prompt_choice(
                title="Grant Permission",
                text="How long should this permission last?",
                informative_text="Choose just once, this session, or always.",
                kind="permission_duration",
                buttons=[
                    ("once", "Just once", QMessageBox.ButtonRole.AcceptRole),
                    ("session", "This session", QMessageBox.ButtonRole.AcceptRole),
                    ("always", "Always", QMessageBox.ButtonRole.AcceptRole),
                    ("deny", "Cancel", QMessageBox.ButtonRole.RejectRole),
                ],
                default_choice="deny",
            )
            if mode_label not in labels:
                if continuation is None:
                    controller.discard_replanning_continuation(outcome)
                self._set_pipeline_state(PipelineState.CANCELLED, "Permission was not granted")
                if cancelled is not None:
                    cancelled()
                return
            grant_type = labels[mode_label]
            target_mode = self._voice_prompt_choice(
                title="Grant Permission",
                text="Which targets should this grant cover?",
                informative_text="Choose only the requested targets or any target.",
                kind="permission_targets",
                buttons=[
                    (
                        "requested_targets",
                        "Only Requested Targets",
                        QMessageBox.ButtonRole.AcceptRole,
                    ),
                    ("any_target", "Any Target", QMessageBox.ButtonRole.AcceptRole),
                    ("deny", "Cancel", QMessageBox.ButtonRole.RejectRole),
                ],
                default_choice="deny",
            )
            if target_mode not in {"requested_targets", "any_target"}:
                if continuation is None:
                    controller.discard_replanning_continuation(outcome)
                self._set_pipeline_state(PipelineState.CANCELLED, "Permission was not granted")
                if cancelled is not None:
                    cancelled()
                return
            grants: dict[tuple[str, str, str, str], set[str]] = {}
            for request in outcome.permission_requests:
                context = request.call.permission_context
                key = (
                    context.user_id,
                    request.call.skill_id,
                    request.scope.capability,
                    context.session_id,
                )
                targets = () if target_mode == "any_target" else request.scope.targets
                grants.setdefault(key, set()).update(targets)
            for (
                user_id,
                skill_id,
                capability,
                session_id,
            ), targets in grants.items():
                container.permissions.grant(
                    user_id=user_id,
                    skill_id=skill_id,
                    capability=capability,
                    grant_type=grant_type,
                    targets=tuple(sorted(targets)),
                    session_id=(session_id if grant_type == GrantType.SESSION else None),
                    correlation_id=str(outcome.call.correlation_id),
                )
            self._reload_permissions()
            self._set_pipeline_state(PipelineState.THINKING, "Preparing the approved action")
            resumes_plan = continuation is None and controller.has_replanning_continuation(outcome)
            worker = AsyncWorker(
                controller.continue_replanning(outcome)
                if resumes_plan
                else resume()
                if resume is not None
                else container.engine.prepare(outcome.call)
            )
            worker.complete.connect(
                self._show_result if resumes_plan else continuation or self._show_continued_outcome
            )
            worker.failed.connect(
                self._show_skill_demo_error if continuation is not None else self._show_error
            )
            worker.finished.connect(lambda: self._discard_worker(worker))
            self._workers.append(worker)
            worker.start()

        def _request_registrations(
            self,
            outcome: Any,
            *,
            continuation: Any | None = None,
            cancelled: Any | None = None,
        ) -> None:
            request = outcome.registration_requests[0]
            decision = self._voice_prompt_choice(
                title="Application Registration Required",
                text="This command needs an application registered first.",
                informative_text=(
                    f"Register an executable for '{request.application}' to continue."
                ),
                kind="approval",
                buttons=[
                    (
                        "approve",
                        "Register Application",
                        QMessageBox.ButtonRole.AcceptRole,
                    ),
                    ("deny", "Not Now", QMessageBox.ButtonRole.RejectRole),
                ],
                default_choice="deny",
            )
            if decision != "approve":
                if continuation is None:
                    controller.discard_replanning_continuation(outcome)
                self._set_pipeline_state(PipelineState.CANCELLED, "Application was not registered")
                if cancelled is not None:
                    cancelled()
                return
            if not self._register_application_alias(request.application):
                if continuation is None:
                    controller.discard_replanning_continuation(outcome)
                self._set_pipeline_state(PipelineState.CANCELLED, "Application was not registered")
                if cancelled is not None:
                    cancelled()
                return
            self._set_pipeline_state(PipelineState.THINKING, "Preparing the registered application")
            resumes_plan = continuation is None and controller.has_replanning_continuation(outcome)
            worker = AsyncWorker(
                controller.continue_replanning(outcome)
                if resumes_plan
                else container.engine.prepare(outcome.call)
            )
            worker.complete.connect(
                self._show_result if resumes_plan else continuation or self._show_continued_outcome
            )
            worker.failed.connect(
                self._show_skill_demo_error if continuation is not None else self._show_error
            )
            worker.finished.connect(lambda: self._discard_worker(worker))
            self._workers.append(worker)
            worker.start()

        def _show_continued_outcome(self, outcome: Any) -> None:
            if outcome.registration_requests:
                message = "The command still needs an application registered."
                self.response.setPlainText(message)
                self.compact_panel.set_response(message)
                self._print_to_terminal(message)
                self._set_response_type(ResponseType.CLARIFICATION)
                self._set_pipeline_state(
                    PipelineState.AWAITING_APPROVAL,
                    "Application registration is required",
                )
                self._request_registrations(outcome)
            elif outcome.permission_requests:
                message = "The command still needs permission for another capability."
                self.response.setPlainText(message)
                self.compact_panel.set_response(message)
                self._print_to_terminal(message)
                self._set_response_type(ResponseType.PERMISSION)
                self._set_pipeline_state(
                    PipelineState.AWAITING_APPROVAL,
                    "Another permission is required",
                )
                self._request_permissions(outcome)
            elif outcome.confirmation:
                self._set_response_type(ResponseType.CONFIRMATION)
                self._request_confirmation(outcome)
            elif outcome.result:
                self._show_skill_result(outcome.result)

        def _request_confirmation(
            self,
            outcome: Any,
            *,
            result_handler: Any | None = None,
            cancelled: Any | None = None,
        ) -> None:
            request = outcome.confirmation
            decision = self._voice_prompt_choice(
                title=f"Confirm {request.risk.value.title()} Risk Action",
                text=request.exact_action,
                informative_text="Approve or deny this action.",
                kind="approval",
                buttons=[
                    ("approve", "Yes", QMessageBox.ButtonRole.AcceptRole),
                    ("deny", "No", QMessageBox.ButtonRole.RejectRole),
                ],
                default_choice="deny",
            )
            if decision != "approve":
                container.confirmations.deny(request.confirmation_id)
                if result_handler is None:
                    controller.discard_replanning_continuation(outcome)
                self._set_pipeline_state(PipelineState.CANCELLED, "Action was not confirmed")
                if cancelled is not None:
                    cancelled()
                return
            typed_token = None
            if request.requires_typed_token:
                expected = str(request.confirmation_id)[-6:]
                typed_token = self._voice_prompt_choice(
                    title="Critical Action Confirmation",
                    text=f"Type {expected} to confirm this critical action.",
                    informative_text="Speak or type the confirmation token.",
                    kind="critical_token",
                    buttons=[
                        ("type", "Type Token", QMessageBox.ButtonRole.AcceptRole),
                        ("deny", "Cancel", QMessageBox.ButtonRole.RejectRole),
                    ],
                    default_choice="deny",
                    expected_token=expected,
                )
                if typed_token == "type":
                    typed_token, ok = QInputDialog.getText(
                        self,
                        "Critical Action Confirmation",
                        f"Type {expected} to confirm this critical action.",
                    )
                    if not ok:
                        typed_token = None
                if typed_token != expected:
                    container.confirmations.deny(request.confirmation_id)
                    if result_handler is None:
                        controller.discard_replanning_continuation(outcome)
                    self._set_pipeline_state(PipelineState.CANCELLED, "Action was not confirmed")
                    if cancelled is not None:
                        cancelled()
                    return
            self._set_pipeline_state(PipelineState.EXECUTING, request.exact_action)
            resumes_plan = result_handler is None and controller.has_replanning_continuation(
                outcome
            )
            worker = AsyncWorker(
                controller.confirm_replanning(outcome, typed_token=typed_token)
                if resumes_plan
                else container.engine.execute(
                    outcome.call,
                    confirmation_id=request.confirmation_id,
                    typed_token=typed_token,
                )
            )
            worker.complete.connect(
                self._show_result if resumes_plan else result_handler or self._show_skill_result
            )
            worker.failed.connect(
                self._show_skill_demo_error if result_handler is not None else self._show_error
            )
            worker.finished.connect(lambda: self._discard_worker(worker))
            self._workers.append(worker)
            worker.start()

        def _show_skill_result(self, result: Any) -> None:
            container.orchestrator.remember_result(result)
            if isinstance(result.data, dict) and "memories" in result.data:
                self._reload_memories_if_visible()
            response = response_from_result(
                result,
                detail=container.orchestrator.response_detail,
                allow_sensitive_speech=container.orchestrator.allow_sensitive_speech,
                hide_sensitive_display=container.orchestrator.hide_sensitive_display,
            )
            self._display_response(response)
            if result.status.value == "completed":
                self._set_pipeline_state(PipelineState.IDLE, "Action completed")
            elif result.status.value == "cancelled":
                self._set_pipeline_state(PipelineState.CANCELLED, result.summary)
            else:
                self._set_pipeline_state(PipelineState.ERROR, result.summary)

        def _display_response(self, response: Any) -> None:
            if response.media_directive is not None:
                self.media_viewer.apply_directive(response.media_directive)
                if response.media_directive.action.value == "show":
                    QTimer.singleShot(0, self._open_media_viewer)
            if response.visual_artifact is not None:
                QTimer.singleShot(
                    0,
                    lambda artifact=response.visual_artifact: self._open_visualization(artifact),
                )
            if not response.updates_display:
                return
            display_text = response.display_text
            sources = []
            if response.citations:
                for index, citation in enumerate(response.citations, 1):
                    reference = citation.url or citation.local_reference or citation.source_id
                    sources.append(f"[{index}] {citation.title} — {reference}")
                display_text += "\n\nSources:\n" + "\n".join(sources)
            self._last_sources_text = "\n".join(sources)
            self.inspect_sources_button.setEnabled(bool(sources))
            self.copy_sources_button.setEnabled(bool(sources))
            self.response.setPlainText(display_text)
            self.compact_panel.set_response(display_text)
            error = response.response_type == ResponseType.ERROR or response.severity == "error"
            self._print_to_terminal(response.display_text, error=error)
            self._set_response_type(response.response_type)

        def _open_media_viewer(self) -> None:
            self.result_window.show_media()

        def _open_visualization(self, artifact: Any) -> None:
            self.result_window.show_visual(artifact)

        def _set_media_fullscreen(self, enabled: bool) -> None:
            self.result_window.set_media_fullscreen(enabled)

        @staticmethod
        def _print_to_terminal(message: str, *, error: bool = False) -> None:
            print(message, file=sys.stderr if error else sys.stdout, flush=True)

        def _set_response_type(self, response_type: ResponseType) -> None:
            labels = {
                ResponseType.ANSWER: "Answer",
                ResponseType.SKILL: "Skill used",
                ResponseType.CLARIFICATION: "Clarification",
                ResponseType.PERMISSION: "Permission request",
                ResponseType.CONFIRMATION: "Confirmation request",
                ResponseType.CONTROL: "Assistant control",
                ResponseType.STATUS: "Status",
                ResponseType.ERROR: "Error",
            }
            label = labels[response_type].upper()
            self.response_type.setText(label)
            self.compact_panel.set_response_type(label)

        def _inspect_sources(self) -> None:
            if not self._last_sources_text:
                self.statusBar().showMessage("This response has no inspectable sources.", 3000)
                return
            box = QMessageBox(self)
            box.setWindowTitle("Response Sources")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText("Sources used for the current response")
            box.setDetailedText(self._last_sources_text)
            box.setAccessibleName("Response source inspector")
            box.exec()

        def _copy_sources(self) -> None:
            if not self._last_sources_text:
                self.statusBar().showMessage("This response has no sources to copy.", 3000)
                return
            QApplication.clipboard().setText(self._last_sources_text)
            self.statusBar().showMessage("Sources copied to the clipboard.", 3000)

        def _apply_accessibility_settings(self, settings: Any) -> None:
            app.setStyleSheet(
                _accessible_stylesheet(
                    base_stylesheet,
                    text_scale=settings.ui_text_scale,
                    high_contrast=settings.ui_high_contrast,
                )
            )
            if settings.ui_reduced_motion:
                self._status_timer.stop()
            elif self._pipeline_is_busy():
                self._status_timer.start()

        def _show_error(self, error: str) -> None:
            self._print_to_terminal(f"Assistant error: {error}", error=True)
            title, action = outcome_guidance(error)
            message = f"{title}.\n\n{error}\n\nNext step: {action}"
            self.response.setPlainText(message)
            self.compact_panel.set_response(message)
            self._set_response_type(ResponseType.ERROR)
            self._set_pipeline_state(PipelineState.ERROR, error)
            if self.isVisible():
                QMessageBox.critical(self, title, f"{error}\n\n{action}")

        def _show_cancelled(self) -> None:
            if self._passive_voice_worker and self._passive_voice_worker.isRunning():
                self._append_activity("Cancelled", "Stopped current command")
                self._set_pipeline_state(PipelineState.IDLE, self._passive_voice_status())
                self._apply_passive_voice_indicators()
                return
            self._set_pipeline_state(PipelineState.CANCELLED, "Stopped by you")

        def cancel(self) -> None:
            container.scheduler.pause()
            self.media_viewer.emergency_stop()

            async def cancel_all():
                await controller.cancel()

            worker = AsyncWorker(cancel_all())
            worker.complete.connect(lambda _: self._show_cancelled())
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def stop_speaking(self) -> None:
            async def stop():
                await controller.stop_speaking()

            worker = AsyncWorker(stop())
            worker.complete.connect(
                lambda _: self._append_activity("Speech stopped", "Stopped assistant speech")
            )
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _position_notification_overlays(self) -> None:
            margin = 16
            drawer_width = min(420, max(320, self.width() - margin * 2))
            drawer_height = min(620, max(260, self.height() - margin * 2))
            self.notification_drawer.setGeometry(
                max(margin, self.width() - drawer_width - margin),
                margin,
                drawer_width,
                drawer_height,
            )
            banner_width = min(500, max(340, self.width() - margin * 2))
            self.notification_banner.setFixedWidth(banner_width)
            self.notification_banner.adjustSize()
            self.notification_banner.move(
                max(margin, self.width() - banner_width - margin),
                margin,
            )
            self.notification_banner.raise_()
            if self.notification_drawer.isVisible():
                self.notification_drawer.raise_()

        def resizeEvent(self, event) -> None:
            super().resizeEvent(event)
            if hasattr(self, "notification_drawer"):
                self._position_notification_overlays()

        def closeEvent(self, event) -> None:
            self._save_current_geometry()
            self._persist_window_state()
            event.ignore()
            self.hide()

        def shutdown(self) -> None:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._save_current_geometry()
            self._persist_window_state()
            container.events.remove_listener(self.hotkey_bridge.pipeline_event.emit)
            container.notification_center.remove_listener(self.hotkey_bridge.notification.emit)
            try:
                import keyboard

                for hotkey in self._hotkeys:
                    keyboard.remove_hotkey(hotkey)
            except Exception:
                pass
            self._hotkeys.clear()
            self.dashboard_page.shutdown()
            for worker in tuple(self._dashboard_workers):
                worker.cancel()
            for worker in tuple(self._dashboard_workers):
                worker.wait(1000)
                self._discard_dashboard_worker(worker)
            self._stop_passive_voice()
            self.result_window.hide()
            self.media_viewer.shutdown()
            if container.wakeword:
                asyncio.run(container.wakeword.cancel())
            with suppress(Exception):
                container.background_runtime.stop()
            with suppress(Exception):
                asyncio.run(controller.cancel())

    _register_windows_app_identity()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Local Assistant")
    app_icon = application_icon()
    app.setWindowIcon(app_icon)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    base_stylesheet = """
        * {
            font-family: "Segoe UI";
            font-size: 10pt;
            color: #dce7f3;
        }
        QMainWindow, QWidget {
            background: #09111f;
        }
        QToolTip {
            background: #17243a;
            color: #f7fbff;
            border: 1px solid #31415b;
            padding: 6px;
        }
        QTabWidget::pane {
            border: 0;
            background: #0c1626;
        }
        QTabBar {
            background: #07101d;
        }
        QTabBar::tab {
            background: transparent;
            color: #8292a8;
            min-width: 126px;
            min-height: 42px;
            padding: 0 18px;
            margin: 3px 8px;
            border-radius: 8px;
            text-align: left;
        }
        QTabBar::tab:hover {
            background: #111e31;
            color: #dce7f3;
        }
        QTabBar::tab:selected {
            background: #162742;
            color: #ffffff;
            border-left: 3px solid #5a8cff;
        }
        #assistantPage {
            background: #0c1626;
        }
        #compactAssistantPage {
            background: #0c1626;
        }
        #eyebrow, #sectionLabel {
            color: #6f92c9;
            font-size: 8pt;
            font-weight: 700;
            letter-spacing: 1px;
        }
        #pageTitle {
            color: #f7fbff;
            font-size: 25pt;
            font-weight: 650;
        }
        #pageSubtitle {
            color: #8fa0b7;
            font-size: 10.5pt;
        }
        #statusCard, #composeCard, #activityCard, #skillBrowserCard, #skillDetailCard,
        #mediaViewer, #compactMediaCard {
            background: #101d30;
            border: 1px solid #1e304a;
            border-radius: 14px;
        }
        #notificationBanner, #notificationDrawer {
            background: #13233a;
            border: 1px solid #345681;
            border-radius: 12px;
        }
        #notificationBanner[severity="success"] {
            border-color: #2b8066;
        }
        #notificationBanner[severity="warning"] {
            border-color: #a47735;
        }
        #notificationBanner[severity="error"] {
            border-color: #9b4960;
        }
        #notificationBannerTitle, #notificationDrawerTitle {
            color: #f7fbff;
            font-size: 12pt;
            font-weight: 650;
        }
        #notificationBannerMessage {
            color: #c7d5e6;
        }
        #notificationUnreadBadge {
            color: #ffffff;
            background: #4f7fd1;
            border-radius: 9px;
            padding: 3px 7px;
            font-weight: 700;
        }
        #notificationDrawerList {
            background: #0b1524;
        }
        #notificationDrawerList::item {
            border-bottom: 1px solid #263952;
            padding: 10px;
        }
        #assistantResultWindow, #resultWindowStack, #visualizationView {
            background: #0c1626;
        }
        #resultWindowTitle {
            color: #f7fbff;
            font-size: 18pt;
            font-weight: 650;
        }
        #resultWindowMetadata {
            color: #8496ad;
        }
        #resultWindowBody {
            background: #0d192a;
            border: 1px solid #263952;
            color: #e8f0fa;
            font-size: 11pt;
            padding: 16px;
        }
        #dashboardPage, #dashboardScroll, #dashboardCardContainer {
            background: #0c1626;
        }
        #dashboardMetricCard {
            background: #101d30;
            border: 1px solid #1e304a;
            border-radius: 14px;
        }
        #dashboardMetricCard[state="stale"], #dashboardMetricCard[state="empty"] {
            border-color: #4e4936;
        }
        #dashboardMetricCard[state="permission_required"] {
            border-color: #6a4b22;
        }
        #dashboardMetricCard[state="error"] {
            border-color: #633747;
        }
        #dashboardCardTitle {
            color: #dce7f3;
            font-size: 10.5pt;
            font-weight: 650;
        }
        #dashboardCardValue {
            color: #f7fbff;
            font-size: 23pt;
            font-weight: 650;
        }
        #dashboardCardState {
            color: #72e2bd;
            background: #15342d;
            border-radius: 7px;
            padding: 3px 7px;
            font-size: 7pt;
            font-weight: 700;
        }
        #dashboardMetricCard[state="stale"] #dashboardCardState,
        #dashboardMetricCard[state="empty"] #dashboardCardState,
        #dashboardMetricCard[state="permission_required"] #dashboardCardState {
            color: #ffd18a;
            background: #3a2a18;
        }
        #dashboardMetricCard[state="error"] #dashboardCardState {
            color: #ff9cac;
            background: #3a1e2a;
        }
        #dashboardCardSummary {
            color: #aebed1;
        }
        #dashboardCardDetails, #dashboardCardSource, #dashboardLastRefresh {
            color: #7f91a8;
            font-size: 8.5pt;
        }
        #dashboardMetricProgress {
            border: 0;
            background: #1c2b40;
            border-radius: 3px;
            max-height: 7px;
        }
        #dashboardMetricProgress::chunk {
            background: #5a8cff;
            border-radius: 3px;
        }
        #statusCard {
            background: #0f2032;
        }
        #statusDot, #compactStatusDot {
            background: #41d6a3;
            border-radius: 5px;
            margin-right: 4px;
        }
        #statusDot[state="thinking"], #statusDot[state="transcribing"],
        #compactStatusDot[state="thinking"], #compactStatusDot[state="transcribing"] {
            background: #73a5ff;
        }
        #statusDot[state="executing"], #statusDot[state="speaking"],
        #compactStatusDot[state="executing"], #compactStatusDot[state="speaking"] {
            background: #b996ff;
        }
        #statusDot[state="listening"], #compactStatusDot[state="listening"] {
            background: #4fd7ef;
        }
        #statusDot[state="awaiting_approval"],
        #compactStatusDot[state="awaiting_approval"] {
            background: #ffbf69;
        }
        #statusDot[state="error"], #statusDot[state="cancelled"],
        #compactStatusDot[state="error"], #compactStatusDot[state="cancelled"] {
            background: #ff6f82;
        }
        #assistantStatus, #compactStatus {
            color: #f7fbff;
            font-size: 12pt;
            font-weight: 650;
        }
        #statusDetail, #statusTranscript, #elapsedStatus, #activityHint,
        #compactStatusDetail, #compactTranscript {
            color: #8496ad;
        }
        #statusTranscript {
            color: #cbd7e6;
            font-size: 9pt;
        }
        #elapsedStatus {
            font-family: "Consolas";
            font-size: 9pt;
        }
        #pipelineProgress, #compactPipelineProgress {
            border: 0;
            background: #1c2b40;
            border-radius: 2px;
        }
        #pipelineProgress::chunk, #compactPipelineProgress::chunk {
            background: #5a8cff;
            border-radius: 2px;
        }
        #micLevel, #compactMicLevel {
            border: 0;
            background: #17263a;
            border-radius: 4px;
        }
        #micLevel::chunk, #compactMicLevel::chunk {
            background: #41d6a3;
            border-radius: 4px;
        }
        #micLevelValue {
            color: #9db0c8;
            font-family: "Consolas";
            font-size: 8.5pt;
        }
        QTextEdit, QLineEdit, QComboBox, QListWidget, QTableWidget {
            background: #0b1524;
            color: #dce7f3;
            border: 1px solid #263952;
            border-radius: 9px;
            padding: 9px;
            selection-background-color: #315ea8;
        }
        QTextEdit:focus, QLineEdit:focus, QComboBox:focus, QListWidget:focus {
            border: 1px solid #5a8cff;
        }
        #commandInput, #compactCommandInput {
            background: #0b1627;
            font-size: 11pt;
            padding: 14px;
        }
        #assistantResponse, #compactAssistantResponse {
            background: #0d192a;
            border: 0;
            color: #e8f0fa;
            font-size: 10.5pt;
            padding: 14px;
        }
        #mediaImageCanvas {
            background: #03070d;
            border: 1px solid #263952;
            border-radius: 8px;
        }
        #mediaVideoSurface {
            background: #03070d;
        }
        #mediaAudioIcon {
            color: #87aef8;
            font-size: 25pt;
            font-weight: 700;
        }
        #mediaViewerTitle, #compactMediaTitle {
            color: #f7fbff;
            font-weight: 650;
        }
        #mediaViewerStatus {
            color: #87aef8;
            font-size: 8pt;
            font-weight: 700;
        }
        #mediaMessageTitle {
            color: #dce7f3;
            font-size: 13pt;
            font-weight: 650;
        }
        #mediaMessageDetail {
            color: #8496ad;
        }
        #responseBadge {
            color: #87aef8;
            background: #172b49;
            border: 1px solid #28456f;
            border-radius: 9px;
            padding: 4px 9px;
            font-size: 7.5pt;
            font-weight: 700;
        }
        #skillDetailScroll, #skillDetailPage {
            background: transparent;
        }
        #skillDetailTitle {
            color: #f7fbff;
            font-size: 21pt;
            font-weight: 650;
        }
        #skillDetailId {
            color: #8292a8;
            font-family: "Consolas";
            font-size: 9pt;
        }
        #skillDescription {
            color: #dce7f3;
            font-size: 10.5pt;
            padding-top: 8px;
        }
        #skillCategoryBadge, #skillSourceBadge, #skillSecurityBadge {
            color: #9fc0ff;
            background: #172b49;
            border: 1px solid #28456f;
            border-radius: 9px;
            padding: 5px 9px;
            font-size: 8pt;
            font-weight: 700;
        }
        #skillSourceBadge[source="extension_hub"] {
            color: #d6b4ff;
            background: #2b2150;
            border-color: #624c9d;
        }
        #skillSourceBadge[source="builtin"] {
            color: #9fc0ff;
            background: #172b49;
            border-color: #28456f;
        }
        #skillSecurityBadge[level="medium"] {
            color: #ffd18a;
            background: #3a2a18;
            border-color: #6a4b22;
        }
        #skillSecurityBadge[level="high"], #skillSecurityBadge[level="critical"] {
            color: #ff9cac;
            background: #3a1e2a;
            border-color: #633747;
        }
        #skillDemoInput, #skillDemoOutput {
            font-family: "Consolas";
            font-size: 9pt;
        }
        #compactLatestRequest {
            color: #cbd7e6;
            background: #101d30;
            border: 1px solid #1e304a;
            border-radius: 8px;
            padding: 8px;
        }
        #compactHeaderButton {
            padding: 6px 10px;
            font-size: 8.5pt;
        }
        #compactActivationMode {
            padding: 6px 8px;
        }
        #wakewordBadge {
            color: #8496ad;
            background: #142238;
            border: 1px solid #263952;
            border-radius: 8px;
            padding: 5px 8px;
            font-size: 7pt;
            font-weight: 700;
        }
        #wakewordBadge[active="true"] {
            color: #72e2bd;
            border-color: #2b6e5a;
        }
        #activityLog {
            background: transparent;
            border: 0;
            padding: 0;
            color: #aebed1;
            font-family: "Consolas";
            font-size: 8.5pt;
        }
        #activityLog::item {
            border-left: 2px solid #2b4262;
            padding: 8px 8px 12px 12px;
        }
        #activityLog::item:selected {
            background: transparent;
            color: #dce7f3;
        }
        QPushButton {
            background: #18283e;
            color: #dce7f3;
            border: 1px solid #2b405e;
            border-radius: 8px;
            padding: 9px 15px;
            font-weight: 600;
        }
        QPushButton:hover {
            background: #203653;
            border-color: #3a587f;
        }
        QPushButton:pressed {
            background: #132238;
        }
        QPushButton:disabled {
            background: #121c2a;
            color: #526175;
            border-color: #1c293b;
        }
        #primaryButton {
            background: #4d7ff0;
            color: #ffffff;
            border: 0;
            padding: 10px 20px;
        }
        #primaryButton:hover {
            background: #6593f5;
        }
        #secondaryButton {
            background: #14253c;
        }
        #dangerButton {
            background: transparent;
            color: #ff8b9a;
            border-color: #633747;
        }
        #dangerButton:hover {
            background: #3a1e2a;
        }
        QTableWidget {
            gridline-color: #1d2d43;
            alternate-background-color: #0e1a2b;
        }
        QHeaderView::section {
            background: #14233a;
            color: #93a7c0;
            border: 0;
            border-bottom: 1px solid #293b54;
            padding: 8px;
            font-weight: 600;
        }
        QScrollBar:vertical {
            background: transparent;
            width: 10px;
            margin: 2px;
        }
        QScrollBar::handle:vertical {
            background: #30435d;
            min-height: 24px;
            border-radius: 5px;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0;
        }
        QStatusBar {
            background: #07101d;
            color: #8292a8;
            border-top: 1px solid #18263a;
        }
        QMessageBox, QDialog {
            background: #101b2c;
        }
        """
    app.setStyleSheet(
        _accessible_stylesheet(
            base_stylesheet,
            text_scale=container.settings.ui_text_scale,
            high_contrast=container.settings.ui_high_contrast,
        )
    )
    window = MainWindow()
    window.setWindowIcon(app_icon)
    app.aboutToQuit.connect(window.shutdown)
    tray = QSystemTrayIcon(app_icon, app)
    menu = QMenu()
    show_action = QAction("Show Companion", app)
    show_action.triggered.connect(window.show_from_tray)
    dashboard_action = QAction("Open Dashboard", app)
    dashboard_action.triggered.connect(window.open_dashboard_from_tray)
    assistant_action = QAction("Open Assistant", app)
    assistant_action.triggered.connect(window.open_assistant_from_tray)
    inbox_action = QAction("Open Inbox", app)
    inbox_action.triggered.connect(window._open_notification_inbox)
    cancel_action = QAction("Emergency Stop", app)
    cancel_action.triggered.connect(window.cancel)
    quit_action = QAction("Quit", app)
    quit_action.triggered.connect(app.quit)
    menu.addAction(show_action)
    menu.addAction(dashboard_action)
    menu.addAction(assistant_action)
    menu.addAction(inbox_action)
    menu.addAction(cancel_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: (
            window.show_from_tray()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick
            else None
        )
    )
    tray.messageClicked.connect(
        lambda: (
            window._open_notification(window._last_tray_notification_id)
            if window._last_tray_notification_id
            else window._open_notification_inbox()
        )
    )
    tray.setToolTip(
        "Local Assistant - voice activation "
        + ("ON" if container.settings.voice_activation_mode != "hotkey" else "off")
    )
    window.tray = tray
    tray.show()
    if container.settings.scheduler_enabled:
        container.background_runtime.start()
    window.show()
    try:
        return app.exec()
    finally:
        instance_mutex.close()
