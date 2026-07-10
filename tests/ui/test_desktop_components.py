from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.bootstrap import build_container
from assistant.ui.desktop.app import run_desktop
from assistant.ui.desktop.controller import DesktopController
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
