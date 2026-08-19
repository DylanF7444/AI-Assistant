from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.bootstrap import build_container
from assistant.contracts import (
    ActivityTrace,
    CitationSourceType,
    PipelineEvent,
    PipelineState,
    Response,
    ResponseCitation,
    ResponseType,
    TracePhase,
    Transcript,
)
from assistant.execution.engine import ExecutionOutcome
from assistant.storage.database import SettingsStore
from assistant.ui.desktop.app import (
    _demo_arguments,
    _group_skill_manifests,
    _skill_source_label,
    run_desktop,
)
from assistant.ui.desktop.controller import DesktopController
from assistant.ui.desktop.state import DesktopWindowState, WindowGeometry
from assistant.ui.desktop.tabs import TAB_GROUPS, TAB_LABELS
from assistant.ui.desktop.workers import AsyncWorker, PassiveVoiceWorker


@pytest.mark.ui
def test_desktop_preserves_all_tab_labels():
    assert len(TAB_LABELS) == 18
    assert {label for labels in TAB_GROUPS.values() for label in labels} == set(TAB_LABELS)


@pytest.mark.ui
def test_skill_groups_sort_categories_and_make_source_explicit():
    manifests = (
        SimpleNamespace(category="Productivity", display_name="Zulu", skill_id="core.zulu"),
        SimpleNamespace(category="Files & Folders", display_name="Reader", skill_id="files.read"),
        SimpleNamespace(category="Productivity", display_name="Alpha", skill_id="core.alpha"),
    )

    groups = _group_skill_manifests(manifests)

    assert [category for category, _ in groups] == ["Files & Folders", "Productivity"]
    assert [manifest.display_name for manifest in groups[1][1]] == ["Alpha", "Zulu"]
    assert _skill_source_label(False) == "Built-in"
    assert _skill_source_label(True) == "Extension Hub"


@pytest.mark.ui
def test_async_worker_reports_completion(qtbot):
    worker = AsyncWorker(asyncio.sleep(0, result="done"))

    with qtbot.waitSignal(worker.complete, timeout=2000) as signal:
        worker.start()

    assert signal.args == ["done"]
    assert worker.wait(2000)


@pytest.mark.ui
def test_async_worker_can_cancel_an_in_flight_coroutine(qtbot):
    worker = AsyncWorker(asyncio.sleep(10))

    worker.start()
    qtbot.waitUntil(worker.isRunning, timeout=1000)
    with qtbot.waitSignal(worker.cancelled, timeout=2000):
        worker.cancel()

    assert worker.wait(2000)


@pytest.mark.ui
@pytest.mark.asyncio
async def test_persistent_voice_ignores_an_empty_final_transcript(settings, monkeypatch):
    container = build_container(settings)
    worker = PassiveVoiceWorker(container, "persistent")
    transcript = Transcript(
        session_id=__import__("uuid").uuid4(),
        raw_text="",
        normalized_text="",
        confidence=None,
    )
    handled = []

    async def transcribe_once(*args, **kwargs):
        return transcript

    async def handle_transcript(value):
        handled.append(value)

    monkeypatch.setattr(container.orchestrator, "transcribe_once", transcribe_once)
    monkeypatch.setattr(container.orchestrator, "handle_transcript", handle_transcript)

    await worker._listen_persistent()

    assert handled == []


@pytest.mark.ui
@pytest.mark.asyncio
async def test_persistent_voice_worker_listens_for_stop_while_request_runs(settings, monkeypatch):
    container = build_container(settings)
    worker = PassiveVoiceWorker(container, "persistent")
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    stop_heard = asyncio.Event()
    initial = Transcript(
        session_id=__import__("uuid").uuid4(),
        raw_text="calculate 1 plus 1",
        normalized_text="calculate 1 plus 1",
    )
    stop = Transcript(
        session_id=__import__("uuid").uuid4(),
        raw_text="stop",
        normalized_text="stop",
    )
    result = ("response", "outcome")

    async def handle_transcript(value):
        assert value is initial
        request_started.set()
        await release_request.wait()
        return result

    async def transcribe_once(*args, **kwargs):
        await request_started.wait()
        return stop

    async def consume_interrupt(value):
        assert value == "stop"
        stop_heard.set()
        release_request.set()
        return True

    monkeypatch.setattr(container.orchestrator, "handle_transcript", handle_transcript)
    monkeypatch.setattr(container.orchestrator, "transcribe_once", transcribe_once)
    monkeypatch.setattr(
        container.orchestrator,
        "consume_persistent_voice_interrupt",
        consume_interrupt,
    )

    assert await worker._run_persistent_request(initial) == result
    assert stop_heard.is_set()


def test_skill_demo_arguments_are_generated_from_the_declared_schema():
    from assistant.skills.builtin import CalculatorSkill

    assert _demo_arguments(CalculatorSkill.manifest.input_schema) == {"expression": "2 + 2"}


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
    assert window.tabs.count() == 18
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

    window._display_response(
        Response(
            display_text="Grounded answer",
            response_type=ResponseType.ANSWER,
            citations=(
                ResponseCitation(
                    source_id="local-1",
                    title="Selected document",
                    source_type=CitationSourceType.LOCAL_DOCUMENT,
                    local_reference="document:abc123",
                ),
            ),
        )
    )
    assert window.inspect_sources_button.isEnabled()
    assert window.copy_sources_button.isEnabled()
    window._copy_sources()
    assert QApplication.clipboard().text() == "[1] Selected document — document:abc123"

    previous_request = window.transcript.toPlainText()
    previous_response = window.response.toPlainText()
    previous_response_type = window.response_type.text()
    window._show_voice_transcript("ambient noise spike")
    window._show_voice_result(
        (
            Response(
                display_text="No speech was detected.",
                response_type=ResponseType.STATUS,
                severity="warning",
                updates_display=False,
            ),
            ExecutionOutcome(),
        )
    )
    assert window.transcript.toPlainText() == previous_request
    assert window.compact_panel.latest_request.text() == previous_request
    assert window.response.toPlainText() == previous_response
    assert window.compact_panel.response.toPlainText() == previous_response
    assert window.response_type.text() == previous_response_type

    window._switch_window_mode("full")
    assert window.view_stack.currentWidget() is window.tabs
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Assistant"
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
    assert window.activity_hint.text() == "Tool names, safe inputs, live actions, and outcomes"
    foreground_state = window._active_state
    correlation_id = __import__("uuid").uuid4()
    window._handle_pipeline_event(
        PipelineEvent(
            correlation_id=correlation_id,
            state=PipelineState.EXECUTING,
            message="Background work",
            trace=ActivityTrace(
                phase=TracePhase.ACTION,
                label="Tool call",
                detail="",
                invocation="background",
                skill_id="web.search",
                tool_name="Web Search",
                action='Search the web for "release notes" (up to 5 results).',
            ),
            updates_foreground_status=False,
        )
    )
    assert window._active_state == foreground_state
    assert "1 ACTIVE" in window.background_activity.text()
    action_text = window.activity_log.item(window.activity_log.count() - 1).text()
    assert "Tool call · Web Search" in action_text
    assert "TOOL    web.search" in action_text
    assert 'ACTION  Search the web for "release notes"' in action_text
    window._handle_pipeline_event(
        PipelineEvent(
            correlation_id=correlation_id,
            state=PipelineState.EXECUTING,
            message="Done",
            trace=ActivityTrace(
                phase=TracePhase.RESULT,
                label="Tool result",
                detail="Found 5 web results.",
                invocation="background",
                skill_id="web.search",
                tool_name="Web Search",
                status="completed",
                duration_ms=1250,
            ),
            updates_foreground_status=False,
        )
    )
    assert window.background_activity.text().endswith("IDLE")
    result_text = window.activity_log.item(window.activity_log.count() - 1).text()
    assert "Tool result · Web Search" in result_text
    assert "STATUS  COMPLETED  ·  1.25s" in result_text
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
    assert persisted.primary_screen == "assistant"

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
    window.setting_text_scale.setCurrentIndex(window.setting_text_scale.findData(1.5))
    window.setting_high_contrast.setChecked(True)
    window.setting_reduced_motion.setChecked(True)
    window._save_settings()
    assert container.settings.wake_word_enabled is True
    assert container.settings.wake_word_model == wake_model
    assert container.settings.wake_word_threshold == 0.73
    assert container.settings.ui_text_scale == 1.5
    assert container.settings.ui_high_contrast is True
    assert container.settings.ui_reduced_motion is True
    assert "font-size: 15pt" in QApplication.instance().styleSheet()

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
def test_automations_tab_scrolls_and_preserves_full_cell_values(qtbot, settings, monkeypatch):
    from PySide6.QtWidgets import QApplication, QHeaderView, QMessageBox, QScrollArea

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
    full_name = "Quarterly automation with a deliberately long saved name"
    container.schedule_store.create_notification(
        name=full_name,
        message="A full message that should remain available to the automation view.",
        run_at=datetime.now(UTC) + timedelta(days=1),
    )
    SettingsStore(container.database).set("first_run_complete", True)

    assert run_desktop(container) == 0
    window = captured_windows[-1]
    qtbot.addWidget(window)
    window._switch_window_mode("full")
    window.resize(820, 600)
    window.tabs.setCurrentIndex(TAB_LABELS.index("Automations"))
    window.show()
    qtbot.waitUntil(lambda: window.automations_scroll.verticalScrollBar().maximum() > 0)

    assert isinstance(window.tabs.currentWidget(), QScrollArea)
    assert window.tabs.currentWidget() is window.automations_scroll
    assert window.automations_scroll.widget().objectName() == "activeScriptsPage"
    assert window.scheduled_scripts.item(0, 1).toolTip() == full_name
    assert (
        window.scheduled_scripts.horizontalHeader().sectionResizeMode(1)
        == QHeaderView.ResizeMode.Stretch
    )

    window.shutdown()
    window.hide()


@pytest.mark.ui
def test_skills_tab_shows_details_permissions_security_and_runs_demo(qtbot, settings, monkeypatch):
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
    asyncio.run(container.extension_hub.install("integration.gmail"))
    container.playbook_store.save_markdown(container.playbook_store.EXAMPLE)
    SettingsStore(container.database).set("first_run_complete", True)

    assert run_desktop(container) == 0
    window = captured_windows[-1]
    qtbot.addWidget(window)

    assert window.playbook_list.count() == 1
    assert "Prepare a release" in window.playbook_list.item(0).text()
    window.playbook_list.setCurrentRow(0)
    window._toggle_playbook()
    assert container.playbook_store.get("prepare-release").enabled is False

    header_rows = [
        window.skill_list.item(row).text()
        for row in range(window.skill_list.count())
        if window.skill_list.item(row).data(Qt.ItemDataRole.UserRole) is None
    ]
    assert any(row.startswith("Productivity  · ") for row in header_rows)
    calculator_item = next(
        window.skill_list.item(row)
        for row in range(window.skill_list.count())
        if window.skill_list.item(row).data(Qt.ItemDataRole.UserRole) == "core.calculator"
    )
    assert "Built-in" in calculator_item.text()

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
    assert window.skill_source_value.text() == "Built-in"
    assert "no special permissions" in window.skill_permissions_summary.text()
    assert '"expression": "2 + 2"' in window.skill_demo_input.toPlainText()
    assert "Background eligible" in window.skill_background_value.text()

    select_skill("integration.gmail")
    assert window.skill_source_value.text() == "Extension Hub"
    gmail_item = next(
        window.skill_list.item(row)
        for row in range(window.skill_list.count())
        if window.skill_list.item(row).data(Qt.ItemDataRole.UserRole) == "integration.gmail"
    )
    assert "Extension Hub" in gmail_item.text()
    select_skill("core.calculator")

    window._run_skill_demo()
    qtbot.waitUntil(
        lambda: "The result is 4" in window.skill_demo_output.toPlainText(),
        timeout=5000,
    )
    assert container.audit.query(event_type="skill_execution")[0]["skill_id"] == ("core.calculator")

    window.shutdown()
    window.hide()
