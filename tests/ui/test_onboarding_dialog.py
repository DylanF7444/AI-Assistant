from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.onboarding import OnboardingStore, SetupStep
from assistant.storage.database import Database, SettingsStore
from assistant.ui.desktop.onboarding_dialog import OnboardingDialog


@pytest.mark.ui
def test_guided_setup_is_accessible_resumable_and_allows_text_only_fallback(qtbot, tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'onboarding-ui.db'}")
    database.migrate()
    store = OnboardingStore(SettingsStore(database))
    dialog = OnboardingDialog(
        store,
        hardware={"processor": "test", "gpu": None, "audio_devices": 0},
        recommendations={"stt": "CPU", "llm": "small local", "tts": "Piper"},
    )
    qtbot.addWidget(dialog)

    assert dialog.accessibleName() == "Local Assistant guided setup"
    assert dialog.progress.accessibleName() == "Guided setup progress"
    assert dialog.pages.count() == 10
    dialog._next()
    dialog._next()
    dialog._next()
    assert dialog.state.current_step == SetupStep.AUDIO
    assert dialog.skip_button.isVisible() is False  # Dialog is not shown yet.
    dialog.show()
    assert dialog.skip_button.isVisible() is True
    dialog._skip()

    restored = OnboardingStore(SettingsStore(database)).get()
    assert restored.current_step == SetupStep.ACTIVATION
    assert SetupStep.AUDIO in restored.skipped_steps
