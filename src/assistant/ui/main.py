from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from assistant.bootstrap import Container
from assistant.contracts import PipelineEvent, PipelineState
from assistant.diagnostics import hardware_report, model_recommendations
from assistant.policy.permissions import GrantType
from assistant.response import response_from_result
from assistant.models import ModelManifest
from assistant.storage.database import SettingsStore
from assistant.storage.macros import MacroDefinition, MacroStep
from assistant.storage.history import HistoryStore
from assistant.audio.capture import enumerate_devices


def run_desktop(container: Container) -> int:
    try:
        from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal
        from PySide6.QtGui import QAction, QIcon, QKeySequence, QShortcut
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
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
            QPushButton,
            QSystemTrayIcon,
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

    class AsyncWorker(QThread):
        complete = Signal(object)
        failed = Signal(str)

        def __init__(self, coroutine: Any) -> None:
            super().__init__()
            self.coroutine = coroutine

        def run(self) -> None:
            try:
                self.complete.emit(asyncio.run(self.coroutine))
            except Exception as error:
                self.failed.emit(str(error))

    class HotkeyBridge(QObject):
        push_to_talk = Signal()
        cancel = Signal()
        pipeline_event = Signal(object)

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Local Assistant")
            self.resize(850, 620)
            self.setAccessibleName("Local Assistant command window")
            self._workers: list[AsyncWorker] = []
            self._hotkeys: list[int] = []
            self.hotkey_bridge = HotkeyBridge()
            self.hotkey_bridge.push_to_talk.connect(self.listen)
            self.hotkey_bridge.cancel.connect(self.cancel)
            self.hotkey_bridge.pipeline_event.connect(self._handle_pipeline_event)
            container.events.add_listener(self.hotkey_bridge.pipeline_event.emit)
            tabs = QTabWidget()
            tabs.addTab(self._command_tab(), "Assistant")
            tabs.addTab(self._skills_tab(), "Skills")
            tabs.addTab(self._permissions_tab(), "Permissions")
            tabs.addTab(self._audit_tab(), "Audit Log")
            tabs.addTab(self._settings_tab(), "Settings")
            tabs.addTab(self._diagnostics_tab(), "Diagnostics")
            tabs.addTab(self._macros_tab(), "Macros")
            tabs.addTab(self._models_tab(), "Models")
            tabs.addTab(self._privacy_tab(), "Privacy")
            tabs.addTab(self._applications_tab(), "Applications")
            self.setCentralWidget(tabs)
            QShortcut(QKeySequence(container.settings.cancel_hotkey), self, self.cancel)
            self._register_global_hotkeys()
            if container.wakeword:
                self._start_wakeword()
            self._first_run()

        def _command_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.status = QLabel("Ready")
            self.status.setAccessibleName("Assistant status")
            self.transcript = QTextEdit()
            self.transcript.setPlaceholderText("Type a command or use push-to-talk")
            self.transcript.setAccessibleName("Command input and transcript")
            controls = QHBoxLayout()
            listen = QPushButton("Push to Talk")
            listen.setAccessibleName("Start voice input")
            listen.clicked.connect(self.listen)
            run = QPushButton("Run Command")
            run.clicked.connect(self.run_command)
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.cancel)
            controls.addWidget(listen)
            controls.addWidget(run)
            controls.addWidget(cancel)
            self.response = QTextEdit()
            self.response.setReadOnly(True)
            self.response.setAccessibleName("Assistant response")
            layout.addWidget(self.status)
            layout.addWidget(self.transcript)
            layout.addLayout(controls)
            layout.addWidget(self.response)
            return page

        def _register_global_hotkeys(self) -> None:
            try:
                import keyboard

                self._hotkeys = [
                    keyboard.add_hotkey(
                        container.settings.push_to_talk_hotkey,
                        self.hotkey_bridge.push_to_talk.emit,
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

        def listen(self) -> None:
            self.status.setText("Listening")
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
            self.status.setText(event.state.value.replace("_", " ").title())
            if event.state == PipelineState.TRANSCRIBING and event.message:
                self.transcript.setPlainText(event.message)

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
                fields = [
                    value["skill_id"],
                    value["capability"],
                    value["grant_type"],
                    "; ".join(value["targets"]),
                    value["effect"],
                ]
                for column, field in enumerate(fields):
                    self.permission_table.setItem(
                        row, column, QTableWidgetItem(str(field))
                    )

        def _grant_permission(self) -> None:
            skill_ids = [item.skill_id for item in container.registry.list()]
            skill_id, ok = QInputDialog.getItem(
                self, "Grant Permission", "Skill", skill_ids, editable=False
            )
            if not ok:
                return
            manifest = container.registry.get(skill_id).manifest
            known_capabilities = {
                "clipboard.read",
                "clipboard.write",
                "device.control",
                "filesystem.read",
                "filesystem.write",
                "input.control",
                "media.control",
                "messaging",
                "network",
                "notification.create",
                "process.launch",
                "process.terminate",
                "process.window",
                "settings.get",
                "settings.set",
            }
            capabilities = sorted(
                known_capabilities
                | {scope.capability for scope in manifest.permissions}
            )
            capability, ok = QInputDialog.getItem(
                self,
                "Grant Permission",
                "Capability",
                capabilities,
                editable=True,
            )
            if not ok or not capability:
                return
            target, ok = QInputDialog.getText(
                self, "Grant Permission", "Optional exact target or approved folder"
            )
            if not ok:
                return
            mode, ok = QInputDialog.getItem(
                self,
                "Grant Permission",
                "Grant mode",
                [GrantType.ONCE.value, GrantType.SESSION.value, GrantType.ALWAYS.value, GrantType.DENY.value],
                editable=False,
            )
            if not ok:
                return
            container.permissions.grant(
                user_id="local-user",
                skill_id=skill_id,
                capability=capability,
                grant_type=GrantType(mode),
                targets=(target,) if target else (),
                session_id="desktop" if mode == GrantType.SESSION.value else None,
                correlation_id="desktop-permission-manager",
            )
            self._reload_permissions()

        def _revoke_permission(self) -> None:
            row = self.permission_table.currentRow()
            if row < 0:
                return
            container.permissions.revoke(
                "local-user",
                self.permission_table.item(row, 0).text(),
                self.permission_table.item(row, 1).text(),
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
            page = QWidget()
            form = QFormLayout(page)
            self.setting_offline = QCheckBox()
            self.setting_offline.setChecked(container.settings.offline_lock)
            self.setting_stt = QLineEdit(container.settings.stt_model)
            self.setting_llm = QLineEdit(container.settings.llm_model)
            self.setting_piper_executable = QLineEdit(
                str(container.settings.piper_executable or "")
            )
            self.setting_piper_model = QLineEdit(
                str(container.settings.piper_model or "")
            )
            self.setting_speech = QComboBox()
            self.setting_speech.addItems(["always", "voice_only", "never"])
            self.setting_speech.setCurrentText(container.settings.speech_mode)
            self.setting_input = QComboBox()
            self.setting_input.addItem("System default", None)
            for device in enumerate_devices():
                if device.input_channels:
                    self.setting_input.addItem(device.name, device.index)
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
            self.setting_hide_sensitive = QCheckBox()
            self.setting_hide_sensitive.setChecked(
                container.settings.hide_sensitive_display
            )
            save = QPushButton("Save Settings")
            save.clicked.connect(self._save_settings)
            form.addRow("Global offline lock", self.setting_offline)
            form.addRow("Input device", self.setting_input)
            form.addRow("STT model", self.setting_stt)
            form.addRow("Local LLM model", self.setting_llm)
            form.addRow("Piper executable", self.setting_piper_executable)
            form.addRow("Piper voice model", self.setting_piper_model)
            form.addRow("Speech mode", self.setting_speech)
            form.addRow("Push-to-talk hotkey", self.setting_ptt)
            form.addRow("Cancel hotkey", self.setting_cancel)
            form.addRow("Transcript retention days", self.setting_retention)
            form.addRow("Diagnostic raw audio", self.setting_diagnostic_audio)
            form.addRow("Noise suppression", self.setting_noise)
            form.addRow("Automatic gain control", self.setting_gain)
            form.addRow("Response detail", self.setting_detail)
            form.addRow("Hide sensitive display results", self.setting_hide_sensitive)
            form.addRow(save)
            return page

        def _save_settings(self) -> None:
            try:
                values = {
                    "offline_lock": self.setting_offline.isChecked(),
                    "audio_input_device": self.setting_input.currentData(),
                    "stt_model": self.setting_stt.text().strip(),
                    "llm_model": self.setting_llm.text().strip(),
                    "piper_executable": self.setting_piper_executable.text().strip()
                    or None,
                    "piper_model": self.setting_piper_model.text().strip() or None,
                    "speech_mode": self.setting_speech.currentText(),
                    "push_to_talk_hotkey": self.setting_ptt.text().strip(),
                    "cancel_hotkey": self.setting_cancel.text().strip(),
                    "retain_transcripts_days": int(
                        self.setting_retention.text().strip()
                    ),
                    "diagnostic_audio": self.setting_diagnostic_audio.isChecked(),
                    "noise_suppression": self.setting_noise.isChecked(),
                    "automatic_gain_control": self.setting_gain.isChecked(),
                    "response_detail": self.setting_detail.currentText(),
                    "hide_sensitive_display": self.setting_hide_sensitive.isChecked(),
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
            container.settings = validated
            container.orchestrator.speech_mode = validated.speech_mode
            container.orchestrator.response_detail = validated.response_detail
            container.orchestrator.hide_sensitive_display = (
                validated.hide_sensitive_display
            )
            container.audio_capture.noise_suppression = (
                validated.noise_suppression
            )
            container.audio_capture.automatic_gain_control = (
                validated.automatic_gain_control
            )
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

        def _applications_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.application_list = QListWidget()
            self._reload_applications()
            controls = QHBoxLayout()
            add = QPushButton("Register Application")
            add.clicked.connect(self._register_application)
            delete = QPushButton("Remove Selected")
            delete.clicked.connect(self._remove_application)
            controls.addWidget(add)
            controls.addWidget(delete)
            layout.addWidget(
                QLabel(
                    "Only applications registered here can be launched by skills."
                )
            )
            layout.addWidget(self.application_list)
            layout.addLayout(controls)
            return page

        def _reload_applications(self) -> None:
            self.application_list.clear()
            for name, path in container.aliases.all("application").items():
                self.application_list.addItem(f"{name} | {path}")

        def _register_application(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select Application", "", "Applications (*.exe)"
            )
            if not path:
                return
            name, ok = QInputDialog.getText(
                self,
                "Application Name",
                "Spoken/display name",
                text=Path(path).stem,
            )
            if ok and name.strip():
                container.aliases.set("application", name, str(Path(path).resolve()))
                self._reload_applications()

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
                return
            self.status.setText(PipelineState.THINKING.value.replace("_", " ").title())
            worker = AsyncWorker(container.orchestrator.handle_text(text))
            worker.complete.connect(self._show_result)
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _show_result(self, payload: Any) -> None:
            response, outcome = payload
            self.response.setPlainText(response.display_text)
            self.status.setText("Ready")
            if outcome.permission_requests:
                self._request_permissions(outcome)
            elif outcome.confirmation:
                self._request_confirmation(outcome)

        def _request_permissions(self, outcome: Any) -> None:
            details = []
            for request in outcome.permission_requests:
                targets = (
                    "\nTargets: " + ", ".join(request.scope.targets)
                    if request.scope.targets
                    else ""
                )
                details.append(
                    f"{request.scope.capability}\n"
                    f"{request.scope.reason}{targets}"
                )
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Permission Required")
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setText("This command needs permission to continue.")
            dialog.setInformativeText("\n\n".join(details))
            grant_button = dialog.addButton(
                "Grant Permission", QMessageBox.ButtonRole.AcceptRole
            )
            dialog.addButton("Not Now", QMessageBox.ButtonRole.RejectRole)
            dialog.exec()
            if dialog.clickedButton() is not grant_button:
                self.status.setText("Permission not granted")
                return
            labels = {
                "Just once": GrantType.ONCE,
                "For this session": GrantType.SESSION,
                "Always": GrantType.ALWAYS,
            }
            mode_label, ok = QInputDialog.getItem(
                self,
                "Grant Permission",
                "How long should this permission last?",
                list(labels),
                editable=False,
            )
            if not ok:
                self.status.setText("Permission not granted")
                return
            grant_type = labels[mode_label]
            grants: dict[tuple[str, str, str, str], set[str]] = {}
            for request in outcome.permission_requests:
                context = request.call.permission_context
                key = (
                    context.user_id,
                    request.call.skill_id,
                    request.scope.capability,
                    context.session_id,
                )
                grants.setdefault(key, set()).update(request.scope.targets)
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
            self.status.setText("Preparing approved command")
            worker = AsyncWorker(container.engine.prepare(outcome.call))
            worker.complete.connect(self._show_continued_outcome)
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda: self._workers.remove(worker))
            self._workers.append(worker)
            worker.start()

        def _show_continued_outcome(self, outcome: Any) -> None:
            if outcome.permission_requests:
                self.response.setPlainText(
                    "The command still needs permission for another capability."
                )
                self.status.setText("Permission required")
                self._request_permissions(outcome)
            elif outcome.confirmation:
                self._request_confirmation(outcome)
            elif outcome.result:
                self._show_skill_result(outcome.result)

        def _request_confirmation(self, outcome: Any) -> None:
            request = outcome.confirmation
            approved = QMessageBox.question(
                self,
                f"Confirm {request.risk.value.title()} Risk Action",
                request.exact_action,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if approved != QMessageBox.StandardButton.Yes:
                container.confirmations.deny(request.confirmation_id)
                self.status.setText("Action not confirmed")
                return
            typed_token = None
            if request.requires_typed_token:
                expected = str(request.confirmation_id)[-6:]
                typed_token, ok = QInputDialog.getText(
                    self,
                    "Critical Action Confirmation",
                    f"Type {expected} to confirm this critical action.",
                )
                if not ok:
                    container.confirmations.deny(request.confirmation_id)
                    self.status.setText("Action not confirmed")
                    return
            self.status.setText("Executing")
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
            self.response.setPlainText(response.display_text)
            self.status.setText(result.status.value.replace("_", " ").title())

        def _show_error(self, error: str) -> None:
            self.status.setText("Error")
            QMessageBox.critical(self, "Assistant Error", error)

        def cancel(self) -> None:
            async def cancel_all():
                await container.audio_capture.stop()
                await container.stt.cancel()
                await container.orchestrator.cancel()

            worker = AsyncWorker(cancel_all())
            worker.complete.connect(lambda _: self.status.setText("Cancelled"))
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
            if container.wakeword:
                asyncio.run(container.wakeword.cancel())
            event.accept()

    app = QApplication(sys.argv)
    app.setApplicationName("Local Assistant")
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
        "Local Assistant - passive listening "
        + ("ON" if container.wakeword else "off")
    )
    tray.show()
    window.show()
    return app.exec()
