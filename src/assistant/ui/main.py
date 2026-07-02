from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from assistant.bootstrap import Container
from assistant.contracts import PipelineEvent, PipelineState, ResponseType
from assistant.diagnostics import hardware_report, model_recommendations
from assistant.policy.permissions import GrantType
from assistant.policy.capabilities import available_capabilities
from assistant.response import response_from_result
from assistant.models import ModelManifest
from assistant.audio.activation import keyword_request, parse_voice_prompt_response
from assistant.storage.database import ItemRow, SettingsStore
from assistant.storage.macros import MacroDefinition, MacroStep
from assistant.storage.history import HistoryStore
from assistant.audio.capture import display_audio_devices
from assistant.storage.secrets import SecretVault
from assistant.windows.adapters import LocalWindowsDeviceAdapter


def run_desktop(container: Container) -> int:
    capability_details = {
        "clipboard.read": ("Read clipboard", "Read the current clipboard contents."),
        "clipboard.write": ("Write clipboard", "Replace the current clipboard contents."),
        "device.control": ("Control devices", "Change or disable connected devices."),
        "device.read": ("Read device information", "Inspect device status and details."),
        "filesystem.read": ("Read files", "Open files or folders for reading."),
        "filesystem.write": ("Change files", "Create, edit, move, rename, or delete files."),
        "input.control": ("Control mouse and keyboard", "Type, click, or automate input."),
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
        from PySide6.QtCore import QObject, QSize, QSettings, Qt, QThread, QTimer, Signal
        from PySide6.QtGui import (
            QAction,
            QIcon,
            QKeySequence,
            QShortcut,
            QTextCursor,
        )
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
            QFrame,
            QFormLayout,
            QFileDialog,
            QHBoxLayout,
            QInputDialog,
            QLabel,
            QLineEdit,
            QListWidget,
            QMainWindow,
            QMenu,
            QMessageBox,
            QProgressBar,
            QPushButton,
            QScrollArea,
            QSizePolicy,
            QSystemTrayIcon,
            QStyle,
            QStyleOptionTab,
            QStylePainter,
            QTabBar,
            QTabWidget,
            QTableWidget,
            QTableWidgetItem,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Desktop UI is not installed. Run: python -m pip install -e .[desktop]"
        ) from exc

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

    class AsyncWorker(QThread):
        complete = Signal(object)
        cancelled = Signal()
        failed = Signal(str)

        def __init__(self, coroutine: Any) -> None:
            super().__init__()
            self.coroutine = coroutine

        def run(self) -> None:
            try:
                self.complete.emit(asyncio.run(self.coroutine))
            except asyncio.CancelledError:
                self.cancelled.emit()
            except Exception as error:
                self.failed.emit(str(error))

    class PassiveVoiceWorker(QThread):
        complete = Signal(object)
        failed = Signal(str)
        partial_transcript = Signal(str)

        def __init__(self, mode: str) -> None:
            super().__init__()
            self.mode = mode
            self._stop_requested = False

        def stop(self) -> None:
            self._stop_requested = True

        def run(self) -> None:
            try:
                asyncio.run(self._run())
            except asyncio.CancelledError:
                return
            except Exception as error:
                self.failed.emit(str(error))

        async def _run(self) -> None:
            while not self._stop_requested:
                if self.mode == "persistent":
                    await self._listen_persistent()
                else:
                    await self._listen_for_activation()

        async def _listen_persistent(self) -> None:
            transcript = await container.orchestrator.transcribe_once(
                container.stt,
                container.audio_capture.chunks(),
                publish_events=False,
                on_transcript=self._show_transcript,
            )
            if self._stop_requested or transcript is None:
                return
            self.complete.emit(await container.orchestrator.handle_transcript(transcript))

        async def _listen_for_activation(self) -> None:
            transcript = await container.orchestrator.transcribe_once(
                container.stt,
                container.audio_capture.chunks(),
                publish_events=False,
                on_transcript=self._show_transcript,
            )
            if self._stop_requested:
                return
            settings = container.settings
            clap_detected = (
                settings.voice_activation_clap_enabled
                and container.audio_capture.consume_clap_detection()
            )
            request = None
            if settings.voice_activation_keyword_enabled and transcript is not None:
                request = keyword_request(
                    transcript.normalized_text,
                    settings.voice_activation_keyword,
                )
            if request is None and not clap_detected:
                return
            if request:
                self.complete.emit(
                    await container.orchestrator.handle_text(
                        request,
                        session_id=str(transcript.session_id)
                        if transcript is not None
                        else "voice-activation",
                        invoked_by_voice=True,
                    )
                )
            else:
                self.complete.emit(
                    await container.orchestrator.listen_once(
                        container.stt,
                        container.audio_capture.chunks(),
                    )
                )

        def _show_transcript(self, transcript: Any) -> None:
            if transcript.raw_text.strip():
                self.partial_transcript.emit(transcript.raw_text)

    class VoicePromptWorker(QThread):
        complete = Signal(str)
        failed = Signal(str)
        partial_transcript = Signal(str)

        def __init__(self) -> None:
            super().__init__()
            self._stop_requested = False

        def stop(self) -> None:
            self._stop_requested = True

        def run(self) -> None:
            try:
                asyncio.run(self._run())
            except asyncio.CancelledError:
                return
            except Exception as error:
                self.failed.emit(str(error))

        async def _run(self) -> None:
            transcript = await container.orchestrator.transcribe_once(
                container.stt,
                container.audio_capture.chunks(),
                publish_events=False,
                on_transcript=self._show_transcript,
            )
            if self._stop_requested or transcript is None:
                return
            self.complete.emit(transcript.normalized_text or transcript.raw_text)

        def _show_transcript(self, transcript: Any) -> None:
            if transcript.raw_text.strip():
                self.partial_transcript.emit(transcript.raw_text)

    class HotkeyBridge(QObject):
        push_to_talk = Signal()
        cancel = Signal()
        pipeline_event = Signal(object)

    class HorizontalTabBar(QTabBar):
        def tabSizeHint(self, index: int) -> QSize:
            size = super().tabSizeHint(index)
            return QSize(size.height(), size.width())

        def paintEvent(self, event) -> None:
            painter = QStylePainter(self)
            option = QStyleOptionTab()
            for index in range(self.count()):
                self.initStyleOption(option, index)
                option.shape = QTabBar.Shape.RoundedNorth
                painter.drawControl(QStyle.ControlElement.CE_TabBarTab, option)

    class MainWindow(QMainWindow):
        ACTIVE_PIPELINE_STATES = {
            PipelineState.LISTENING,
            PipelineState.TRANSCRIBING,
            PipelineState.THINKING,
            PipelineState.EXECUTING,
            PipelineState.SPEAKING,
        }

        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Local Assistant")
            self.resize(1080, 760)
            self.setMinimumSize(820, 600)
            self.setAccessibleName("Local Assistant command window")
            self._workers: list[AsyncWorker] = []
            self._hotkeys: list[int] = []
            self._shortcuts: list[QShortcut] = []
            self._passive_voice_worker: PassiveVoiceWorker | None = None
            self._passive_voice_restart_enabled = False
            self._voice_prompt_active = False
            self._activity_started = time.perf_counter()
            self._status_frame = 0
            self._active_state = PipelineState.IDLE
            self.hotkey_bridge = HotkeyBridge()
            self.hotkey_bridge.push_to_talk.connect(self.listen)
            self.hotkey_bridge.cancel.connect(self.cancel)
            self.hotkey_bridge.pipeline_event.connect(self._handle_pipeline_event)
            container.events.add_listener(self.hotkey_bridge.pipeline_event.emit)
            tabs = QTabWidget()
            tabs.setTabBar(HorizontalTabBar())
            tabs.addTab(self._command_tab(), "Assistant")
            tabs.addTab(self._active_scripts_tab(), "Active Scripts")
            tabs.addTab(self._skills_tab(), "Skills")
            tabs.addTab(self._permissions_tab(), "Permissions")
            tabs.addTab(self._audit_tab(), "Audit Log")
            tabs.addTab(self._settings_tab(), "Settings")
            tabs.addTab(self._diagnostics_tab(), "Diagnostics")
            tabs.addTab(self._macros_tab(), "Macros")
            tabs.addTab(self._models_tab(), "Models")
            tabs.addTab(self._knowledge_tab(), "Knowledge")
            tabs.addTab(self._privacy_tab(), "Privacy")
            tabs.addTab(self._applications_tab(), "Applications")
            tabs.setTabPosition(QTabWidget.TabPosition.West)
            tabs.setDocumentMode(True)
            self.setCentralWidget(tabs)
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
            self._add_shortcut("Ctrl+Return", self.run_command)
            self._register_global_hotkeys()
            if container.wakeword:
                self._start_wakeword()
            self._start_passive_voice()
            self._first_run()

        def _active_scripts_tab(self) -> QWidget:
            page = QWidget()
            page.setObjectName("activeScriptsPage")
            layout = QVBoxLayout(page)
            layout.setContentsMargins(28, 24, 28, 24)
            layout.setSpacing(12)

            header = QHBoxLayout()
            heading = QVBoxLayout()
            title = QLabel("Active scripts")
            title.setObjectName("pageTitle")
            subtitle = QLabel(
                "Live assistant jobs, future reminders, and runtime hooks."
            )
            subtitle.setObjectName("pageSubtitle")
            heading.addWidget(title)
            heading.addWidget(subtitle)
            refresh = QPushButton("Refresh")
            refresh.clicked.connect(self._refresh_active_scripts)
            header.addLayout(heading, 1)
            header.addWidget(refresh)
            layout.addLayout(header)

            running_label = QLabel("RUNNING NOW")
            running_label.setObjectName("sectionLabel")
            self.running_scripts = QTableWidget(0, 5)
            self.running_scripts.setHorizontalHeaderLabels(
                ["Type", "Name", "Details", "Started", "Elapsed"]
            )
            self.running_scripts.setSelectionBehavior(
                QTableWidget.SelectionBehavior.SelectRows
            )
            self.running_scripts.setEditTriggers(
                QTableWidget.EditTrigger.NoEditTriggers
            )
            self.running_scripts.setAlternatingRowColors(True)
            self.running_scripts.horizontalHeader().setStretchLastSection(True)
            cancel = QPushButton("Cancel selected")
            cancel.setObjectName("dangerButton")
            cancel.clicked.connect(self._cancel_selected_script)

            scheduled_label = QLabel("SCHEDULED")
            scheduled_label.setObjectName("sectionLabel")
            self.scheduled_scripts = QTableWidget(0, 4)
            self.scheduled_scripts.setHorizontalHeaderLabels(
                ["Type", "Name", "Due", "Status"]
            )
            self.scheduled_scripts.setEditTriggers(
                QTableWidget.EditTrigger.NoEditTriggers
            )
            self.scheduled_scripts.setAlternatingRowColors(True)
            self.scheduled_scripts.horizontalHeader().setStretchLastSection(True)

            hooks_label = QLabel("HOOKS")
            hooks_label.setObjectName("sectionLabel")
            self.script_hooks = QTableWidget(0, 3)
            self.script_hooks.setHorizontalHeaderLabels(
                ["Hook", "Trigger", "Status"]
            )
            self.script_hooks.setEditTriggers(
                QTableWidget.EditTrigger.NoEditTriggers
            )
            self.script_hooks.setAlternatingRowColors(True)
            self.script_hooks.horizontalHeader().setStretchLastSection(True)

            layout.addWidget(running_label)
            layout.addWidget(self.running_scripts, 2)
            layout.addWidget(cancel)
            layout.addWidget(scheduled_label)
            layout.addWidget(self.scheduled_scripts, 2)
            layout.addWidget(hooks_label)
            layout.addWidget(self.script_hooks, 1)
            self._refresh_active_scripts()
            return page

        @staticmethod
        def _set_table_row(table: QTableWidget, row: int, values: list[str]) -> None:
            table.insertRow(row)
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))

        def _refresh_active_scripts(self) -> None:
            if not hasattr(self, "running_scripts"):
                return
            selected_id = None
            selected = self.running_scripts.currentRow()
            if selected >= 0:
                item = self.running_scripts.item(selected, 0)
                selected_id = item.data(Qt.ItemDataRole.UserRole) if item else None

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
            with container.database.session() as session:
                reminders = list(
                    session.scalars(
                        select(ItemRow)
                        .where(
                            ItemRow.kind == "reminder",
                            ItemRow.completed.is_(False),
                        )
                        .order_by(ItemRow.due_at)
                    )
                )
            for row, reminder in enumerate(reminders):
                due = reminder.due_at
                if due is None:
                    due_text = "No due time"
                    status = "Pending"
                else:
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=UTC)
                    due_text = due.astimezone().strftime("%b %d, %Y %I:%M %p")
                    status = "Overdue" if due < now else "Scheduled"
                self._set_table_row(
                    self.scheduled_scripts,
                    row,
                    ["Reminder", reminder.title, due_text, status],
                )

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
                    if self._passive_voice_worker
                    and self._passive_voice_worker.isRunning()
                    else "Hotkey only",
                ),
                ("Pipeline events", "Assistant activity", "Active"),
            ]
            self.script_hooks.setRowCount(0)
            for row, hook in enumerate(hooks):
                self._set_table_row(self.script_hooks, row, list(hook))

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

        def _command_tab(self) -> QWidget:
            page = QWidget()
            page.setObjectName("assistantPage")
            layout = QVBoxLayout(page)
            layout.setContentsMargins(32, 28, 32, 28)
            layout.setSpacing(18)

            eyebrow = QLabel("PRIVATE  |  LOCAL  |  READY")
            eyebrow.setObjectName("eyebrow")
            title = QLabel("What can I help with?")
            title.setObjectName("pageTitle")
            subtitle = QLabel(
                "Type a request or use your microphone. Actions and approvals "
                "will appear here as they happen."
            )
            subtitle.setObjectName("pageSubtitle")
            subtitle.setWordWrap(True)
            layout.addWidget(eyebrow)
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
            self.run_button.clicked.connect(self.run_command)
            self.cancel_button = QPushButton("Stop")
            self.cancel_button.setObjectName("dangerButton")
            self.cancel_button.clicked.connect(self.cancel)
            self.cancel_button.setEnabled(False)
            controls.addWidget(self.listen_button)
            controls.addStretch()
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
            self.response_type = QLabel("READY")
            self.response_type.setObjectName("responseBadge")
            self.response_type.setAccessibleName("Assistant response type")
            response_header.addWidget(response_label)
            response_header.addStretch()
            response_header.addWidget(self.response_type)
            compose_layout.addWidget(input_label)
            compose_layout.addWidget(self.transcript)
            compose_layout.addLayout(controls)
            compose_layout.addSpacing(4)
            compose_layout.addLayout(response_header)
            compose_layout.addWidget(self.response)

            activity_card = QFrame()
            activity_card.setObjectName("activityCard")
            activity_card.setMinimumWidth(250)
            activity_card.setMaximumWidth(330)
            activity_layout = QVBoxLayout(activity_card)
            activity_layout.setContentsMargins(18, 18, 18, 18)
            activity_layout.setSpacing(10)
            activity_title = QLabel("ACTIVITY")
            activity_title.setObjectName("sectionLabel")
            activity_hint = QLabel("Live progress and actions")
            activity_hint.setObjectName("activityHint")
            self.activity_log = QListWidget()
            self.activity_log.setObjectName("activityLog")
            self.activity_log.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.activity_log.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            activity_layout.addWidget(activity_title)
            activity_layout.addWidget(activity_hint)
            activity_layout.addWidget(self.activity_log)
            self._append_activity("Ready", "Waiting for your request")

            content.addWidget(compose_card, 3)
            content.addWidget(activity_card, 1)
            layout.addLayout(content, 1)
            return page

        def _append_activity(self, label: str, detail: str = "") -> None:
            elapsed = time.perf_counter() - self._activity_started
            timestamp = f"{elapsed:04.1f}s"
            text = f"{timestamp}   {label}"
            if detail and detail.lower() != label.lower():
                text += f"\n          {detail}"
            self.activity_log.addItem(text)
            self.activity_log.scrollToBottom()

        def _set_pipeline_state(
            self,
            state: PipelineState,
            message: str = "",
            progress: float | None = None,
        ) -> None:
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
            self._status_frame = 0
            self.status.setText(labels[state])
            self.status_detail.setText(message or {
                PipelineState.IDLE: "Waiting for your request",
                PipelineState.THINKING: "Understanding what you asked",
                PipelineState.EXECUTING: "A local skill is running",
            }.get(state, labels[state]))
            self.status_dot.setProperty("state", state.value)
            self.status_dot.style().unpolish(self.status_dot)
            self.status_dot.style().polish(self.status_dot)
            self.run_button.setEnabled(not active)
            self.listen_button.setEnabled(not active)
            self.cancel_button.setEnabled(active)
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
                if progress is None:
                    self.pipeline_progress.setRange(0, 0)
                else:
                    self.pipeline_progress.setRange(0, 100)
                    self.pipeline_progress.setValue(round(progress * 100))
                self._status_timer.start()
            else:
                self._status_timer.stop()
                self.elapsed_status.setText("")
                self.pipeline_progress.hide()
            self._append_activity(labels[state], message)

        def _pipeline_is_busy(self) -> bool:
            return self._active_state in self.ACTIVE_PIPELINE_STATES

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
                self.status.setText(base + "." * self._status_frame)
                elapsed = time.perf_counter() - self._activity_started
                self.elapsed_status.setText(f"{elapsed:.1f}s")

        def _update_mic_level(self) -> None:
            level = min(1.0, max(0.0, container.audio_capture.level))
            threshold = max(container.audio_capture.vad_threshold, 0.001)
            self._set_mic_level_value(
                round(min(1.0, level / (threshold * 4)) * 100)
            )

        def _set_mic_level_value(self, value: int) -> None:
            value = max(0, min(100, value))
            self.mic_level.setValue(value)
            self.mic_level_value.setText(f"{value}%")

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
            worker = PassiveVoiceWorker(container.settings.voice_activation_mode)
            worker.complete.connect(self._show_result)
            worker.failed.connect(self._show_error)
            worker.partial_transcript.connect(self._show_voice_transcript)
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
            if (
                self._passive_voice_worker
                and self._passive_voice_worker.isRunning()
            ):
                self.status.setText("Listening")
                self.status_dot.setProperty("state", PipelineState.LISTENING.value)
                self.status_dot.style().unpolish(self.status_dot)
                self.status_dot.style().polish(self.status_dot)
                self.status_detail.setText(self._passive_voice_status())
                self.listen_button.setEnabled(False)
                self._mic_level_timer.start()

        def _passive_voice_status(self) -> str:
            if container.settings.voice_activation_mode == "persistent":
                return "Persistent voice mode is listening"
            if container.settings.voice_activation_mode == "keyword_clap":
                triggers = []
                if container.settings.voice_activation_keyword_enabled:
                    triggers.append(
                        f'"{container.settings.voice_activation_keyword}"'
                    )
                if container.settings.voice_activation_clap_enabled:
                    triggers.append("a clap")
                return "Listening for " + " or ".join(triggers)
            return "Waiting for your request"

        def listen(self) -> None:
            if self._pipeline_is_busy():
                self.statusBar().showMessage("Voice input is already active.", 1500)
                return
            self._set_pipeline_state(PipelineState.LISTENING, "Microphone is active")
            worker = AsyncWorker(
                container.orchestrator.listen_once(
                    container.stt, container.audio_capture.chunks()
                )
            )
            worker.complete.connect(self._show_result)
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _handle_pipeline_event(self, event: PipelineEvent) -> None:
            self._set_pipeline_state(event.state, event.message, event.progress)
            if event.state == PipelineState.TRANSCRIBING and event.message:
                self.transcript.setPlainText(event.message)
                self._set_status_transcript(event.message)

        def _show_voice_transcript(self, text: str) -> None:
            self.transcript.setPlainText(text)
            self.transcript.moveCursor(QTextCursor.MoveOperation.End)
            self._set_status_transcript(text)

        def _set_status_transcript(self, text: str) -> None:
            text = " ".join(text.split())
            self.status_transcript.setText(f"Heard: {text}" if text else "")

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
            restart_passive = (
                container.settings.voice_activation_mode != "hotkey"
                and self._passive_voice_restart_enabled
            )
            self._voice_prompt_active = True
            self._stop_passive_voice()
            choice = {"value": default_choice}
            button_choices: dict[Any, str] = {}
            dialog = QMessageBox(self)
            dialog.setWindowTitle(title)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setText(text)
            dialog.setInformativeText(informative_text)
            for value, label, role in buttons:
                button_choices[dialog.addButton(label, role)] = value

            worker = VoicePromptWorker()

            def accept_spoken(transcript: str) -> None:
                self._set_status_transcript(transcript)
                response = parse_voice_prompt_response(
                    transcript,
                    kind,
                    expected_token=expected_token,
                )
                if response is None:
                    self.statusBar().showMessage(
                        "I did not catch a valid prompt response.", 1800
                    )
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
            if restart_passive:
                QTimer.singleShot(250, self._start_passive_voice)
            return choice["value"]

        def _discard_worker(self, worker: Any) -> None:
            if worker in self._workers:
                self._workers.remove(worker)

        def _skills_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.skill_list = QListWidget()
            for manifest in container.registry.list():
                self.skill_list.addItem(
                    f"{'Enabled' if container.registry.is_enabled(manifest.skill_id) else 'Disabled'} | "
                    f"{manifest.display_name} ({manifest.skill_id}) - {manifest.risk.value}"
                )
            toggle = QPushButton("Enable / Disable Selected")
            toggle.clicked.connect(self._toggle_skill)
            install = QPushButton("Install Signed Skill")
            install.clicked.connect(self._install_skill)
            update = QPushButton("Update Selected Skill")
            update.clicked.connect(self._update_skill)
            health = QPushButton("Check Selected Health")
            health.clicked.connect(self._check_skill_health)
            uninstall = QPushButton("Uninstall Selected Skill")
            uninstall.clicked.connect(self._uninstall_skill)
            layout.addWidget(self.skill_list)
            layout.addWidget(toggle)
            layout.addWidget(install)
            layout.addWidget(update)
            layout.addWidget(health)
            layout.addWidget(uninstall)
            return page

        def _reload_skills(self) -> None:
            self.skill_list.clear()
            for manifest in container.registry.list():
                self.skill_list.addItem(
                    f"{'Enabled' if container.registry.is_enabled(manifest.skill_id) else 'Disabled'} | "
                    f"{manifest.display_name} ({manifest.skill_id}) - {manifest.risk.value}"
                )

        def _toggle_skill(self) -> None:
            row = self.skill_list.currentRow()
            if row < 0:
                return
            manifest = container.registry.list()[row]
            if container.registry.is_enabled(manifest.skill_id):
                container.registry.disable(manifest.skill_id)
            else:
                container.registry.enable(manifest.skill_id)
            self.skill_list.item(row).setText(
                f"{'Enabled' if container.registry.is_enabled(manifest.skill_id) else 'Disabled'} | "
                f"{manifest.display_name} ({manifest.skill_id}) - {manifest.risk.value}"
            )

        def _install_skill(self) -> None:
            package = QFileDialog.getExistingDirectory(
                self, "Select Signed Skill Package"
            )
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
            row = self.skill_list.currentRow()
            manifests = container.registry.list()
            return manifests[row] if 0 <= row < len(manifests) else None

        def _update_skill(self) -> None:
            selected = self._selected_manifest()
            if selected is None:
                return
            package = QFileDialog.getExistingDirectory(
                self, "Select Signed Skill Update"
            )
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
                QLabel(
                    "Skills start with no sensitive permissions. Grants can be one-time, "
                    "session-only, always, or deny, and may be target-scoped."
                )
            )
            self.permission_table = QTableWidget(0, 5)
            self.permission_table.setHorizontalHeaderLabels(
                ["Skill", "Capability", "Mode", "Targets", "Effect"]
            )
            self.permission_table.horizontalHeader().setStretchLastSection(True)
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
                row = self.permission_table.rowCount()
                self.permission_table.insertRow(row)
                manifest = container.registry.get(value["skill_id"]).manifest
                fields = [
                    f"{manifest.display_name} ({friendly_identifier(value['skill_id'])})",
                    capability_table_label(value["capability"]),
                    grant_type_label(value["grant_type"]),
                    target_summary(value["targets"]),
                    value["effect"],
                ]
                for column, field in enumerate(fields):
                    item = QTableWidgetItem(str(field))
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, value["skill_id"])
                    elif column == 1:
                        item.setData(Qt.ItemDataRole.UserRole, value["capability"])
                    self.permission_table.setItem(row, column, item)

        def _grant_permission(self) -> None:
            skill_choices = {
                skill_choice_label(item): item.skill_id
                for item in container.registry.list()
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
            }
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

        def _audit_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            table = QTableWidget(0, 4)
            table.setHorizontalHeaderLabels(["Time", "Event", "Skill", "Decision"])
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
            clear = QPushButton("Delete Audit Log")
            clear.clicked.connect(lambda: (container.audit.clear(), table.setRowCount(0)))
            layout.addWidget(table)
            layout.addWidget(clear)
            return page

        def _settings_tab(self) -> QWidget:
            page = QScrollArea()
            page.setWidgetResizable(True)
            content = QWidget()
            form = QFormLayout(content)
            self.setting_offline = QCheckBox()
            self.setting_offline.setChecked(container.settings.offline_lock)
            self.setting_stt = QLineEdit(container.settings.stt_model)
            self.setting_llm = QLineEdit(container.settings.llm_model)
            self.setting_openai_model = QLineEdit(container.settings.openai_model)
            self.setting_openai_codex_model = QLineEdit(
                container.settings.openai_codex_model
            )
            self.setting_anthropic_model = QLineEdit(
                container.settings.anthropic_model
            )
            vault = SecretVault(container.settings.data_dir / "secrets")
            self.setting_openai_key = QLineEdit()
            self.setting_openai_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.setting_openai_key.setPlaceholderText(
                "Stored; enter a new key to replace"
                if vault.has("openai")
                else "OpenAI API key"
            )
            self.setting_anthropic_key = QLineEdit()
            self.setting_anthropic_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.setting_anthropic_key.setPlaceholderText(
                "Stored; enter a new key to replace"
                if vault.has("anthropic")
                else "Anthropic API key"
            )
            self.setting_brave_key = QLineEdit()
            self.setting_brave_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.setting_brave_key.setPlaceholderText(
                "Stored; enter a new key to replace"
                if vault.has("brave")
                else "Brave Search API key"
            )
            self.setting_search_provider = QComboBox()
            self.setting_search_provider.addItem(
                "DuckDuckGo Instant Answer", "duckduckgo"
            )
            self.setting_search_provider.addItem("Brave Search", "brave")
            selected = self.setting_search_provider.findData(
                container.settings.search_provider
            )
            self.setting_search_provider.setCurrentIndex(max(0, selected))
            self.setting_piper_executable = QLineEdit(
                str(container.settings.piper_executable or "")
            )
            self.setting_piper_model = QLineEdit(
                str(container.settings.piper_model or "")
            )
            self.setting_piper_executable_label = QLabel("Piper executable")
            self.setting_piper_model_label = QLabel("Piper voice model")
            self.setting_speech = QComboBox()
            self.setting_speech.addItems(["always", "voice_only", "never"])
            self.setting_speech.setCurrentText(container.settings.speech_mode)
            self.setting_voice_activation_mode = QComboBox()
            self.setting_voice_activation_mode.addItem("Hotkey only", "hotkey")
            self.setting_voice_activation_mode.addItem(
                "Keyword or clap", "keyword_clap"
            )
            self.setting_voice_activation_mode.addItem(
                "Persistent voice", "persistent"
            )
            selected = self.setting_voice_activation_mode.findData(
                container.settings.voice_activation_mode
            )
            self.setting_voice_activation_mode.setCurrentIndex(max(0, selected))
            self.setting_voice_keyword_enabled = QCheckBox()
            self.setting_voice_keyword_enabled.setChecked(
                container.settings.voice_activation_keyword_enabled
            )
            self.setting_voice_keyword = QLineEdit(
                container.settings.voice_activation_keyword
            )
            self.setting_voice_keyword_enabled_label = QLabel(
                "Enable activation keyword"
            )
            self.setting_voice_keyword_label = QLabel("Activation keyword")
            self.setting_voice_clap_enabled = QCheckBox()
            self.setting_voice_clap_enabled.setChecked(
                container.settings.voice_activation_clap_enabled
            )
            self.setting_voice_clap_threshold = QLineEdit(
                str(container.settings.voice_activation_clap_threshold)
            )
            self.setting_voice_clap_enabled_label = QLabel("Enable clap activation")
            self.setting_voice_clap_threshold_label = QLabel(
                "Clap sensitivity threshold"
            )
            self.setting_input = QComboBox()
            self.setting_input.addItem("System default", None)
            for device in display_audio_devices(input_only=True):
                self.setting_input.addItem(device.label, device.index)
            selected = self.setting_input.findData(
                container.settings.audio_input_device
            )
            self.setting_input.setCurrentIndex(max(0, selected))
            self.setting_ptt = QLineEdit(container.settings.push_to_talk_hotkey)
            self.setting_cancel = QLineEdit(container.settings.cancel_hotkey)
            self.setting_retention = QLineEdit(
                str(container.settings.retain_transcripts_days)
            )
            self.setting_diagnostic_audio = QCheckBox()
            self.setting_diagnostic_audio.setChecked(
                container.settings.diagnostic_audio
            )
            self.setting_noise = QCheckBox()
            self.setting_noise.setChecked(container.settings.noise_suppression)
            self.setting_gain = QCheckBox()
            self.setting_gain.setChecked(
                container.settings.automatic_gain_control
            )
            self.setting_detail = QComboBox()
            self.setting_detail.addItems(["short", "normal", "detailed"])
            self.setting_detail.setCurrentText(container.settings.response_detail)
            self.setting_show_reasoning = QCheckBox()
            self.setting_show_reasoning.setChecked(
                container.settings.show_reasoning_steps
            )
            self.setting_terminal_debug = QCheckBox()
            self.setting_terminal_debug.setChecked(
                container.settings.terminal_debug
            )
            self.setting_hide_sensitive = QCheckBox()
            self.setting_hide_sensitive.setChecked(
                container.settings.hide_sensitive_display
            )
            self.setting_confirmation_prompts = QTableWidget(
                len(confirmation_prompt_actions), 2
            )
            self.setting_confirmation_prompts.setHorizontalHeaderLabels(
                ["Action", "Prompt"]
            )
            self.setting_confirmation_prompts.setEditTriggers(
                QTableWidget.EditTrigger.NoEditTriggers
            )
            self.setting_confirmation_prompt_rows = []
            for row, (key, label, default) in enumerate(confirmation_prompt_actions):
                checked = container.settings.confirmation_overrides.get(key, default)
                self.setting_confirmation_prompts.setItem(
                    row, 0, QTableWidgetItem(label)
                )
                checkbox = QCheckBox()
                checkbox.setChecked(checked)
                checkbox.setAccessibleName(f"Require prompt for {label}")
                self.setting_confirmation_prompts.setCellWidget(row, 1, checkbox)
                self.setting_confirmation_prompt_rows.append((key, default, checkbox))
            self.setting_confirmation_prompts.horizontalHeader().setStretchLastSection(
                True
            )
            self.setting_confirmation_prompts.setMinimumHeight(260)
            save = QPushButton("Save Settings")
            save.clicked.connect(self._save_settings)
            form.addRow("Global offline lock", self.setting_offline)
            form.addRow("Input device", self.setting_input)
            form.addRow("STT model", self.setting_stt)
            form.addRow("Local LLM model", self.setting_llm)
            form.addRow("OpenAI model", self.setting_openai_model)
            form.addRow("OpenAI Codex model", self.setting_openai_codex_model)
            form.addRow("OpenAI API key", self.setting_openai_key)
            form.addRow("Anthropic model", self.setting_anthropic_model)
            form.addRow("Anthropic API key", self.setting_anthropic_key)
            form.addRow("Web search provider", self.setting_search_provider)
            form.addRow("Brave Search API key", self.setting_brave_key)
            form.addRow(
                self.setting_piper_executable_label, self.setting_piper_executable
            )
            form.addRow(self.setting_piper_model_label, self.setting_piper_model)
            form.addRow("Speech mode", self.setting_speech)
            form.addRow("Voice activation", self.setting_voice_activation_mode)
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
            form.addRow("Diagnostic raw audio", self.setting_diagnostic_audio)
            form.addRow("Noise suppression", self.setting_noise)
            form.addRow("Automatic gain control", self.setting_gain)
            form.addRow("Response detail", self.setting_detail)
            form.addRow("Show request breakdown", self.setting_show_reasoning)
            form.addRow("Terminal debug output", self.setting_terminal_debug)
            form.addRow("Hide sensitive display results", self.setting_hide_sensitive)
            form.addRow("Require yes/no prompts", self.setting_confirmation_prompts)
            form.addRow(save)
            self.setting_speech.currentTextChanged.connect(
                lambda _text: self._update_voice_settings_visibility()
            )
            self.setting_voice_activation_mode.currentIndexChanged.connect(
                lambda _index: self._update_voice_settings_visibility()
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
            self.setting_piper_executable_label.setVisible(speech_enabled)
            self.setting_piper_executable.setVisible(speech_enabled)
            self.setting_piper_model_label.setVisible(speech_enabled)
            self.setting_piper_model.setVisible(speech_enabled)

            activation_mode = self.setting_voice_activation_mode.currentData()
            keyword_clap_active = activation_mode == "keyword_clap"
            keyword_active = (
                keyword_clap_active and self.setting_voice_keyword_enabled.isChecked()
            )
            clap_active = (
                keyword_clap_active and self.setting_voice_clap_enabled.isChecked()
            )

            self.setting_voice_keyword_enabled_label.setVisible(keyword_clap_active)
            self.setting_voice_keyword_enabled.setVisible(keyword_clap_active)
            self.setting_voice_keyword_label.setVisible(keyword_active)
            self.setting_voice_keyword.setVisible(keyword_active)
            self.setting_voice_clap_enabled_label.setVisible(keyword_clap_active)
            self.setting_voice_clap_enabled.setVisible(keyword_clap_active)
            self.setting_voice_clap_threshold_label.setVisible(clap_active)
            self.setting_voice_clap_threshold.setVisible(clap_active)

        def _save_settings(self) -> None:
            try:
                confirmation_overrides = {
                    key: checkbox.isChecked()
                    for key, default, checkbox in self.setting_confirmation_prompt_rows
                    if checkbox.isChecked() != default
                }
                values = {
                    "offline_lock": self.setting_offline.isChecked(),
                    "audio_input_device": self.setting_input.currentData(),
                    "stt_model": self.setting_stt.text().strip(),
                    "llm_model": self.setting_llm.text().strip(),
                    "openai_model": self.setting_openai_model.text().strip(),
                    "openai_codex_model": (
                        self.setting_openai_codex_model.text().strip()
                    ),
                    "anthropic_model": self.setting_anthropic_model.text().strip(),
                    "search_provider": self.setting_search_provider.currentData(),
                    "piper_executable": self.setting_piper_executable.text().strip()
                    or None,
                    "piper_model": self.setting_piper_model.text().strip() or None,
                    "speech_mode": self.setting_speech.currentText(),
                    "voice_activation_mode": (
                        self.setting_voice_activation_mode.currentData()
                    ),
                    "voice_activation_keyword_enabled": (
                        self.setting_voice_keyword_enabled.isChecked()
                    ),
                    "voice_activation_keyword": (
                        self.setting_voice_keyword.text().strip()
                    ),
                    "voice_activation_clap_enabled": (
                        self.setting_voice_clap_enabled.isChecked()
                    ),
                    "voice_activation_clap_threshold": float(
                        self.setting_voice_clap_threshold.text().strip()
                    ),
                    "push_to_talk_hotkey": self.setting_ptt.text().strip(),
                    "cancel_hotkey": self.setting_cancel.text().strip(),
                    "retain_transcripts_days": int(
                        self.setting_retention.text().strip()
                    ),
                    "diagnostic_audio": self.setting_diagnostic_audio.isChecked(),
                    "noise_suppression": self.setting_noise.isChecked(),
                    "automatic_gain_control": self.setting_gain.isChecked(),
                    "response_detail": self.setting_detail.currentText(),
                    "show_reasoning_steps": self.setting_show_reasoning.isChecked(),
                    "terminal_debug": self.setting_terminal_debug.isChecked(),
                    "hide_sensitive_display": self.setting_hide_sensitive.isChecked(),
                    "confirmation_overrides": confirmation_overrides,
                }
                validated = container.settings.model_copy(update=values)
                validated = type(container.settings).model_validate(
                    validated.model_dump()
                )
            except Exception as error:
                QMessageBox.critical(self, "Invalid Settings", str(error))
                return
            persisted = validated.model_dump(
                mode="json",
                exclude={
                    "data_dir",
                    "database_name",
                    "profile",
                    "trusted_model_keys",
                    "trusted_skill_keys",
                },
            )
            SettingsStore(container.database).set(
                "application_settings", persisted
            )
            vault = SecretVault(validated.data_dir / "secrets")
            for name, field in (
                ("openai", self.setting_openai_key),
                ("anthropic", self.setting_anthropic_key),
                ("brave", self.setting_brave_key),
            ):
                if field.text().strip():
                    vault.set(name, field.text())
                    field.clear()
                    field.setPlaceholderText("Stored; enter a new key to replace")
            container.settings = validated
            container.orchestrator.speech_mode = validated.speech_mode
            container.orchestrator.response_detail = validated.response_detail
            container.orchestrator.show_reasoning_steps = (
                validated.show_reasoning_steps
            )
            container.orchestrator.terminal_debug = validated.terminal_debug
            container.orchestrator.hide_sensitive_display = (
                validated.hide_sensitive_display
            )
            container.engine.confirmation_overrides = dict(
                validated.confirmation_overrides
            )
            container.audio_capture.noise_suppression = (
                validated.noise_suppression
            )
            container.audio_capture.automatic_gain_control = (
                validated.automatic_gain_control
            )
            container.audio_capture.clap_detector.threshold = (
                validated.voice_activation_clap_threshold
            )
            self._start_passive_voice()
            QMessageBox.information(
                self,
                "Settings Saved",
                "Settings were saved. Model, device, and global hotkey changes "
                "take full effect after restart.",
            )

        def _diagnostics_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            report = hardware_report()
            recommendations = model_recommendations(report)
            recent_metrics = container.metrics.recent(100)
            command_latencies = [
                item["duration_ms"]
                for item in recent_metrics
                if item["stage"] == "execution" and item["duration_ms"] is not None
            ]
            self.diagnostic_text = QTextEdit()
            self.diagnostic_text.setReadOnly(True)
            self.diagnostic_text.setPlainText(
                "\n".join(
                    [
                        f"OS: {report['os']}",
                        f"Python: {report['python']}",
                        f"Processor: {report['processor']}",
                        f"GPU: {report['gpu'] or 'Not detected'}",
                        f"Audio endpoints: {report['audio_devices']}",
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
                    ]
                )
            )
            benchmark = QPushButton("Benchmark Deterministic Command")
            benchmark.clicked.connect(self._benchmark)
            layout.addWidget(self.diagnostic_text)
            layout.addWidget(benchmark)
            return page

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
                QLabel(
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
                    f"{definition.name}: {len(definition.steps)} step(s) - "
                    f"{definition.description}"
                )

        def _edit_macro(self) -> None:
            name, ok = QInputDialog.getText(self, "Macro", "Macro name")
            if not ok or not name.strip():
                return
            description, ok = QInputDialog.getText(
                self, "Macro", "Description"
            )
            if not ok:
                return
            example = (
                '[{"skill_id":"core.calculator",'
                '"arguments":{"expression":"2 + 2"}}]'
            )
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
                container.macro_store.save(
                    MacroDefinition(name, description, steps)
                )
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
            if selected is None or not self._confirm(
                f'Delete knowledge base "{selected.name}"?'
            ):
                return
            container.knowledge_bases.delete(selected.id)
            self._reload_knowledge()

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
                ("Clear TTS Cache", lambda: self._clear_directory("tts-cache")),
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
            layout.addWidget(
                QLabel(
                    "Only applications registered here can be launched by skills."
                )
            )
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
                return await LocalWindowsDeviceAdapter().search_installed_apps(
                    limit=500
                )

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

        def _register_application_alias(
            self, requested_name: str | None = None
        ) -> bool:
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
            Path(destination).write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )

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
                manifest = ModelManifest.model_validate_json(
                    Path(manifest_path).read_text("utf-8")
                )
                destination = container.model_manager.install_from_file(
                    Path(model_path), manifest
                )
            except Exception as error:
                QMessageBox.critical(self, "Model Installation Failed", str(error))
                return
            QMessageBox.information(
                self, "Model Installed", f"Installed to {destination}"
            )
            self._reload_models()

        def _select_model_location(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select Existing Local Model"
            )
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
                SettingsStore(container.database).set(
                    f"model_location.{component}", path
                )
                QMessageBox.information(
                    self, "Model Location Saved", f"{component}: {path}"
                )

        def _first_run(self) -> None:
            store = SettingsStore(container.database)
            if store.get("first_run_complete", False):
                return
            report = hardware_report()
            recommendations = model_recommendations(report)
            QMessageBox.information(
                self,
                "Welcome to Local Assistant",
                "Hardware detection is complete.\n\n"
                f"GPU: {report['gpu'] or 'Not detected'}\n"
                f"Recommended STT: {recommendations['stt']}\n"
                f"Recommended LLM: {recommendations['llm']}\n"
                f"Recommended TTS: {recommendations['tts']}\n\n"
                "Use the Models tab to install signed local model files.",
            )
            store.set("first_run_complete", True)

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
            self.activity_log.clear()
            self._activity_started = time.perf_counter()
            self._set_pipeline_state(
                PipelineState.THINKING, "Interpreting your request"
            )
            worker = AsyncWorker(container.orchestrator.handle_text(text))
            worker.complete.connect(self._show_result)
            worker.cancelled.connect(self._show_cancelled)
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _show_result(self, payload: Any) -> None:
            response, outcome = payload
            self._display_response(response)
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
                    if self._passive_voice_worker
                    and self._passive_voice_worker.isRunning()
                    else "Request complete",
                )
                self._apply_passive_voice_indicators()

        def _request_permissions(self, outcome: Any) -> None:
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
                self._set_pipeline_state(
                    PipelineState.CANCELLED, "Permission was not granted"
                )
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
                self._set_pipeline_state(
                    PipelineState.CANCELLED, "Permission was not granted"
                )
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
                self._set_pipeline_state(
                    PipelineState.CANCELLED, "Permission was not granted"
                )
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
                    session_id=(
                        session_id
                        if grant_type == GrantType.SESSION
                        else None
                    ),
                    correlation_id=str(outcome.call.correlation_id),
                )
            self._reload_permissions()
            self._set_pipeline_state(
                PipelineState.THINKING, "Preparing the approved action"
            )
            worker = AsyncWorker(container.engine.prepare(outcome.call))
            worker.complete.connect(self._show_continued_outcome)
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _request_registrations(self, outcome: Any) -> None:
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
                self._set_pipeline_state(
                    PipelineState.CANCELLED, "Application was not registered"
                )
                return
            if not self._register_application_alias(request.application):
                self._set_pipeline_state(
                    PipelineState.CANCELLED, "Application was not registered"
                )
                return
            self._set_pipeline_state(
                PipelineState.THINKING, "Preparing the registered application"
            )
            worker = AsyncWorker(container.engine.prepare(outcome.call))
            worker.complete.connect(self._show_continued_outcome)
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _show_continued_outcome(self, outcome: Any) -> None:
            if outcome.registration_requests:
                message = "The command still needs an application registered."
                self.response.setPlainText(message)
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

        def _request_confirmation(self, outcome: Any) -> None:
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
                self._set_pipeline_state(
                    PipelineState.CANCELLED, "Action was not confirmed"
                )
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
                    self._set_pipeline_state(
                        PipelineState.CANCELLED, "Action was not confirmed"
                    )
                    return
            self._set_pipeline_state(
                PipelineState.EXECUTING, request.exact_action
            )
            worker = AsyncWorker(
                container.engine.execute(
                    outcome.call,
                    confirmation_id=request.confirmation_id,
                    typed_token=typed_token,
                )
            )
            worker.complete.connect(self._show_skill_result)
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _show_skill_result(self, result: Any) -> None:
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
            self.response.setPlainText(response.display_text)
            error = (
                response.response_type == ResponseType.ERROR
                or response.severity == "error"
            )
            self._print_to_terminal(response.display_text, error=error)
            self._set_response_type(response.response_type)

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
            self.response_type.setText(labels[response_type].upper())

        def _show_error(self, error: str) -> None:
            self._print_to_terminal(f"Assistant error: {error}", error=True)
            self._set_response_type(ResponseType.ERROR)
            self._set_pipeline_state(PipelineState.ERROR, error)
            QMessageBox.critical(self, "Assistant Error", error)

        def _show_cancelled(self) -> None:
            if (
                self._passive_voice_worker
                and self._passive_voice_worker.isRunning()
            ):
                self._append_activity("Cancelled", "Stopped current command")
                self._set_pipeline_state(PipelineState.IDLE, self._passive_voice_status())
                self._apply_passive_voice_indicators()
                return
            self._set_pipeline_state(PipelineState.CANCELLED, "Stopped by you")

        def cancel(self) -> None:
            async def cancel_all():
                await container.audio_capture.stop()
                await container.stt.cancel()
                await container.orchestrator.cancel()

            worker = AsyncWorker(cancel_all())
            worker.complete.connect(lambda _: self._show_cancelled())
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def closeEvent(self, event) -> None:
            container.events.remove_listener(self.hotkey_bridge.pipeline_event.emit)
            try:
                import keyboard

                for hotkey in self._hotkeys:
                    keyboard.remove_hotkey(hotkey)
            except Exception:
                pass
            self._stop_passive_voice()
            if container.wakeword:
                asyncio.run(container.wakeword.cancel())
            event.accept()

    app = QApplication(sys.argv)
    app.setApplicationName("Local Assistant")
    app.setStyle("Fusion")
    app.setStyleSheet(
        """
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
        #statusCard, #composeCard, #activityCard {
            background: #101d30;
            border: 1px solid #1e304a;
            border-radius: 14px;
        }
        #statusCard {
            background: #0f2032;
        }
        #statusDot {
            background: #41d6a3;
            border-radius: 5px;
            margin-right: 4px;
        }
        #statusDot[state="thinking"], #statusDot[state="transcribing"] {
            background: #73a5ff;
        }
        #statusDot[state="executing"], #statusDot[state="speaking"] {
            background: #b996ff;
        }
        #statusDot[state="listening"] {
            background: #4fd7ef;
        }
        #statusDot[state="awaiting_approval"] {
            background: #ffbf69;
        }
        #statusDot[state="error"], #statusDot[state="cancelled"] {
            background: #ff6f82;
        }
        #assistantStatus {
            color: #f7fbff;
            font-size: 12pt;
            font-weight: 650;
        }
        #statusDetail, #statusTranscript, #elapsedStatus, #activityHint {
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
        #pipelineProgress {
            border: 0;
            background: #1c2b40;
            border-radius: 2px;
        }
        #pipelineProgress::chunk {
            background: #5a8cff;
            border-radius: 2px;
        }
        #micLevel {
            border: 0;
            background: #17263a;
            border-radius: 4px;
        }
        #micLevel::chunk {
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
        #commandInput {
            background: #0b1627;
            font-size: 11pt;
            padding: 14px;
        }
        #assistantResponse {
            background: #0d192a;
            border: 0;
            color: #e8f0fa;
            font-size: 10.5pt;
            padding: 14px;
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
    )
    window = MainWindow()
    tray = QSystemTrayIcon(QIcon(), app)
    menu = QMenu()
    show_action = QAction("Show", app)
    show_action.triggered.connect(window.show)
    cancel_action = QAction("Emergency Stop", app)
    cancel_action.triggered.connect(window.cancel)
    quit_action = QAction("Quit", app)
    quit_action.triggered.connect(app.quit)
    menu.addAction(show_action)
    menu.addAction(cancel_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.setToolTip(
        "Local Assistant - voice activation "
        + ("ON" if container.settings.voice_activation_mode != "hotkey" else "off")
    )
    tray.show()
    window.show()
    return app.exec()
