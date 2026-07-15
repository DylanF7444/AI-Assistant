from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.bootstrap import build_container
from assistant.storage.database import SettingsStore
from assistant.ui.desktop.app import _demo_arguments, run_desktop
from assistant.ui.desktop.controller import DesktopController
from assistant.ui.desktop.state import DesktopWindowState, WindowGeometry
from assistant.ui.desktop.tabs import TAB_GROUPS, TAB_LABELS
from assistant.ui.desktop.workers import AsyncWorker


@pytest.mark.ui
def test_desktop_preserves_all_tab_labels():
    assert len(TAB_LABELS) == 14
    assert {label for labels in TAB_GROUPS.values() for label in labels} == set(TAB_LABELS)


@pytest.mark.ui
def test_async_worker_reports_completion(qtbot):
    worker = AsyncWorker(asyncio.sleep(0, result="done"))

    with qtbot.waitSignal(worker.complete, timeout=2000) as signal:
        worker.start()

    assert signal.args == ["done"]
    assert worker.wait(2000)


def test_skill_demo_arguments_are_generated_from_the_declared_schema():
    from assistant.skills.builtin import CalculatorSkill

    assert _demo_arguments(CalculatorSkill.manifest.input_schema) == {
        "expression": "2 + 2"
    }


@pytest.mark.ui
def test_desktop_defers_first_run_dialog_until_event_loop(settings, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args: messages.append(str(args[1])),
    )
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)

    assert run_desktop(build_container(settings)) == 0
    assert messages == []


@pytest.mark.ui
@pytest.mark.asyncio
async def test_desktop_controller_runs_text_command(settings):
    controller = DesktopController(build_container(settings))

    response, outcome = await controller.run_text("calculate 17 times 19")

    assert "323" in response.display_text
    assert outcome.result is not None


@pytest.mark.ui
def test_compact_companion_switches_views_renders_and_hides_to_tray(
    qtbot, settings, tmp_path, monkeypatch
):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QHeaderView, QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    captured_windows = []
    existing_windows = {id(widget) for widget in QApplication.topLevelWidgets()}

    def capture_window(self):
        captured_windows.extend(
            widget
            for widget in QApplication.topLevelWidgets()
            if widget.objectName() == "localAssistantWindow" and id(widget) not in existing_windows
        )
        return 0

    monkeypatch.setattr(QApplication, "exec", capture_window)
    container = build_container(settings)
    container.audit.record(
        correlation_id="desktop-audit-layout-test",
        event_type="permission_scope_evaluation",
        skill_id="windows.application_window",
        decision="confirmation_required",
    )
    store = SettingsStore(container.database)
    store.set("first_run_complete", True)

    assert run_desktop(container) == 0
    window = captured_windows[-1]
    qtbot.addWidget(window)

    assert window._window_mode == "compact"
    assert window.view_stack.currentWidget() is window.compact_panel
    assert window.tabs.count() == 14
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert (window.minimumWidth(), window.minimumHeight()) == (380, 500)
    assert (window.maximumWidth(), window.maximumHeight()) == (520, 680)

    offscreen = window._bounded_window_geometry(
        WindowGeometry(x=1_000_000, y=1_000_000, width=400, height=520),
        default_width=400,
        default_height=520,
    )
    assert any(
        screen.availableGeometry().intersects(offscreen) for screen in QApplication.screens()
    )

    window.compact_panel.composer.setPlainText("calculate 17 times 19")
    window.compact_panel._submit()
    qtbot.waitUntil(lambda: "323" in window.compact_panel.response.toPlainText(), timeout=5000)

    assert window.compact_panel.composer.toPlainText() == ""
    assert "calculate 17 times 19" in window.compact_panel.latest_request.text()
    assert window.response.toPlainText() == window.compact_panel.response.toPlainText()

    window._switch_window_mode("full")
    assert window.view_stack.currentWidget() is window.tabs
    tab_bar = window.tabs.tabBar()
    longest_tab_width = max(
        tab_bar.fontMetrics().horizontalAdvance(window.tabs.tabText(index))
        for index in range(window.tabs.count())
    )
    assert tab_bar.width() >= longest_tab_width
    assert (
        tab_bar.tabRect(window.tabs.indexOf(window.permission_table.parentWidget())).width()
        >= longest_tab_width
    )
    compose_layout = window.response.parentWidget().layout()
    assert compose_layout.indexOf(window.response) < compose_layout.indexOf(window.transcript)
    assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert window.maximumWidth() == window.UNBOUNDED_WIDGET_SIZE
    assert window.maximumHeight() == window.UNBOUNDED_WIDGET_SIZE
    assert window.audit_table.objectName() == "auditTable"
    assert all(
        window.audit_table.horizontalHeader().sectionResizeMode(column)
        == QHeaderView.ResizeMode.ResizeToContents
        for column in range(window.audit_table.columnCount())
    )
    assert all(
        window.audit_table.columnWidth(column) >= window.audit_table.sizeHintForColumn(column)
        for column in range(window.audit_table.columnCount())
    )
    for table in (
        window.permission_table,
        window.file_access_grants_table,
        window.file_access_policy_table,
        window.memory_table,
    ):
        assert all(
            table.horizontalHeader().sectionResizeMode(column)
            == QHeaderView.ResizeMode.ResizeToContents
            for column in range(table.columnCount())
        )
    persisted = DesktopWindowState.model_validate(store.get("desktop_window_state", {}))
    assert persisted.mode == "full"

    restarts = []
    monkeypatch.setattr(window, "_start_passive_voice", lambda: restarts.append(True))
    window._set_voice_activation_mode("keyword_clap")
    assert container.settings.voice_activation_mode == "keyword_clap"
    assert restarts == [True]

    wake_model = tmp_path / "wake.onnx"
    wake_model.write_bytes(b"model")
    window.setting_wake_word_enabled.setChecked(True)
    window.setting_wake_word_model.setText(str(wake_model))
    window.setting_wake_word_threshold.setText("0.73")
    window._save_settings()
    assert container.settings.wake_word_enabled is True
    assert container.settings.wake_word_model == wake_model
    assert container.settings.wake_word_threshold == 0.73

    window._switch_window_mode("compact")
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert window._shutdown_started is False
    window.show_from_tray()
    assert window.isVisible()

    window.shutdown()
    window.shutdown()
    assert window._shutdown_started is True
    window.hide()


@pytest.mark.ui
def test_skills_tab_shows_details_permissions_security_and_runs_demo(
    qtbot, settings, monkeypatch
):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    captured_windows = []
    existing_windows = {id(widget) for widget in QApplication.topLevelWidgets()}

    def capture_window(self):
        captured_windows.extend(
            widget
            for widget in QApplication.topLevelWidgets()
            if widget.objectName() == "localAssistantWindow" and id(widget) not in existing_windows
        )
        return 0

    monkeypatch.setattr(QApplication, "exec", capture_window)
    container = build_container(settings)
    SettingsStore(container.database).set("first_run_complete", True)

    assert run_desktop(container) == 0
    window = captured_windows[-1]
    qtbot.addWidget(window)

    def select_skill(skill_id: str) -> None:
        for row in range(window.skill_list.count()):
            item = window.skill_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == skill_id:
                window.skill_list.setCurrentItem(item)
                return
        raise AssertionError(f"Skill not found: {skill_id}")

    select_skill("windows.file_search")
    assert window.skill_category_value.text() == "Files & Folders"
    assert "Search filenames" in window.skill_description.text()
    assert "Read files" in window.skill_permission_table.item(0, 0).text()
    assert "selected folder" in window.skill_permission_table.item(0, 2).text()

    select_skill("windows.launcher")
    assert window.skill_security_value.text().startswith("Medium")

    select_skill("core.calculator")
    assert window.skill_category_value.text() == "Productivity"
    assert "no special permissions" in window.skill_permissions_summary.text()
    assert '"expression": "2 + 2"' in window.skill_demo_input.toPlainText()

    window._run_skill_demo()
    qtbot.waitUntil(
        lambda: "The result is 4" in window.skill_demo_output.toPlainText(),
        timeout=5000,
    )
    assert container.audit.query(event_type="skill_execution")[0]["skill_id"] == (
        "core.calculator"
    )

    window.shutdown()
    window.hide()
