from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.bootstrap import build_container
from assistant.storage.database import SettingsStore
from assistant.ui.desktop.app import run_desktop


@pytest.mark.ui
def test_voice_settings_switch_between_local_file_and_elevenlabs_setup(
    qtbot,
    settings,
    monkeypatch,
):
    from PySide6.QtWidgets import QApplication

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

    assert window.setting_tts_provider.currentData() == "piper"
    assert window.setting_piper_executable_row.isHidden() is False
    assert window.setting_piper_model_row.isHidden() is False
    assert window.setting_elevenlabs_key.isHidden() is True

    window.setting_tts_provider.setCurrentIndex(window.setting_tts_provider.findData("elevenlabs"))

    assert window.setting_piper_executable_row.isHidden() is True
    assert window.setting_piper_model_row.isHidden() is True
    assert window.setting_elevenlabs_key.isHidden() is False
    assert window.setting_elevenlabs_voice_id.isHidden() is False
    assert window.setting_elevenlabs_model.isHidden() is False

    window.setting_speech.setCurrentText("never")
    assert window.setting_elevenlabs_key.isHidden() is True
    assert window.setting_elevenlabs_voice_id.isHidden() is True
    assert window.setting_elevenlabs_model.isHidden() is True

    window.shutdown()
