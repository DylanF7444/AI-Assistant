from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from assistant.onboarding import SETUP_STEPS, OnboardingStore, SetupStep


class OnboardingDialog(QDialog):
    def __init__(
        self,
        store: OnboardingStore,
        *,
        hardware: dict,
        recommendations: dict[str, str],
        actions: dict[SetupStep, Callable[[], None]] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.actions = actions or {}
        self.state = store.start()
        self.setWindowTitle("Local Assistant setup")
        self.setAccessibleName("Local Assistant guided setup")
        self.setModal(False)
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        title = QLabel("Set up Local Assistant")
        title.setObjectName("onboardingTitle")
        title.setAccessibleName("Guided setup title")
        layout.addWidget(title)
        self.progress = QProgressBar()
        self.progress.setRange(0, len(SETUP_STEPS))
        self.progress.setAccessibleName("Guided setup progress")
        layout.addWidget(self.progress)
        body = QHBoxLayout()
        self.step_list = QListWidget()
        self.step_list.setAccessibleName("Guided setup steps")
        self.step_list.setMaximumWidth(220)
        self.pages = QStackedWidget()
        self.pages.setAccessibleName("Current guided setup step")
        body.addWidget(self.step_list)
        body.addWidget(self.pages, 1)
        layout.addLayout(body, 1)
        page_text = _page_text(hardware, recommendations)
        for index, step in enumerate(SETUP_STEPS):
            item = QListWidgetItem(f"{index + 1}. {step.value.replace('_', ' ').title()}")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.step_list.addItem(item)
            page = QWidget()
            page_layout = QVBoxLayout(page)
            heading = QLabel(item.text())
            heading.setAccessibleName(f"Setup step {index + 1} heading")
            description = QLabel(page_text[step])
            description.setWordWrap(True)
            description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard)
            description.setAccessibleName(f"Setup step {index + 1} instructions")
            page_layout.addWidget(heading)
            page_layout.addWidget(description)
            action = self.actions.get(step)
            if action is not None:
                button = QPushButton(_action_label(step))
                button.setAccessibleName(_action_label(step))
                button.clicked.connect(action)
                page_layout.addWidget(button)
            page_layout.addStretch()
            self.pages.addWidget(page)
        controls = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.back_button.setAccessibleName("Go to previous setup step")
        self.back_button.clicked.connect(self._back)
        self.later_button = QPushButton("Continue later")
        self.later_button.setAccessibleName("Save setup progress and continue later")
        self.later_button.clicked.connect(self.reject)
        self.skip_button = QPushButton("Skip with text-only fallback")
        self.skip_button.setAccessibleName("Skip unavailable audio step with text-only fallback")
        self.skip_button.clicked.connect(self._skip)
        self.next_button = QPushButton("Complete step")
        self.next_button.setAccessibleName("Complete current setup step")
        self.next_button.clicked.connect(self._next)
        for button in (self.back_button, self.later_button, self.skip_button, self.next_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        self._show_step(self.state.current_step)

    def _show_step(self, step: SetupStep) -> None:
        index = SETUP_STEPS.index(step)
        self.pages.setCurrentIndex(index)
        self.step_list.setCurrentRow(index)
        self.progress.setValue(len(self.state.completed_steps) + len(self.state.skipped_steps))
        self.back_button.setEnabled(index > 0)
        self.skip_button.setVisible(step in {SetupStep.AUDIO, SetupStep.VOICE_TEST})
        self.next_button.setText("Finish setup" if step == SetupStep.FINISH else "Complete step")
        self.next_button.setFocus()

    def _back(self) -> None:
        index = max(0, self.pages.currentIndex() - 1)
        self.state = self.store.set_current_step(SETUP_STEPS[index])
        self._show_step(SETUP_STEPS[index])

    def _skip(self) -> None:
        step = SETUP_STEPS[self.pages.currentIndex()]
        self.state = self.store.skip_step(
            step, "Audio hardware or voice provider is not currently available."
        )
        self._show_step(self.state.current_step)

    def _next(self) -> None:
        step = SETUP_STEPS[self.pages.currentIndex()]
        try:
            if step == SetupStep.FINISH:
                self.state = self.store.finish()
                self.progress.setValue(len(SETUP_STEPS))
                self.accept()
                return
            self.state = self.store.complete_step(step)
            self._show_step(self.state.current_step)
        except ValueError as exc:
            QMessageBox.warning(self, "Setup is not complete", str(exc))


def _action_label(step: SetupStep) -> str:
    return {
        SetupStep.MODELS: "Open Models",
        SetupStep.AUDIO: "Open voice settings",
        SetupStep.ACTIVATION: "Open voice settings",
        SetupStep.PERMISSIONS: "Open Permissions",
        SetupStep.TYPED_TEST: "Run typed test",
        SetupStep.VOICE_TEST: "Start voice test",
        SetupStep.SAFETY: "Try Emergency Stop",
        SetupStep.FINISH: "Open Diagnostics",
    }.get(step, "Open")


def _page_text(hardware: dict, recommendations: dict[str, str]) -> dict[SetupStep, str]:
    return {
        SetupStep.PRIVACY: (
            "Core commands, planning, speech recognition, and document analysis can remain local. "
            "Network features are optional, separately permissioned, and disabled by Offline Lock."
        ),
        SetupStep.HARDWARE: (
            f"Detected processor: {hardware.get('processor') or 'unknown'}\n"
            f"GPU: {hardware.get('gpu') or 'not detected'}\n"
            f"Audio endpoints: {hardware.get('audio_devices', 0)}"
        ),
        SetupStep.MODELS: (
            f"Recommended STT: {recommendations['stt']}\n"
            f"Recommended LLM: {recommendations['llm']}\n"
            f"Recommended TTS: {recommendations['tts']}\n"
            "Install signed models or select existing local files, then run Diagnostics."
        ),
        SetupStep.AUDIO: (
            "Choose the microphone and output device. Verify the input level moves and run a "
            "short output test. You can continue in text-only mode if audio is unavailable."
        ),
        SetupStep.ACTIVATION: (
            "Configure push-to-talk first. Optional wake-word or clap activation remains local and "
            "can be disabled at any time."
        ),
        SetupStep.PERMISSIONS: (
            "Permissions are denied by default. Grants name the capability, exact target, duration, "
            "and action. Sensitive or consequential actions also require confirmation."
        ),
        SetupStep.TYPED_TEST: (
            "Run the deterministic calculator test. Confirm that the result appears and that Cancel "
            "remains available during work."
        ),
        SetupStep.VOICE_TEST: (
            "Start a voice request and say: calculate seventeen times nineteen. Confirm the live "
            "transcript and text result. Skip explicitly if voice is unavailable."
        ),
        SetupStep.SAFETY: (
            "Cancel stops the foreground request. Emergency Stop additionally pauses automations, "
            "capture, speech, media, and active jobs until you deliberately resume them."
        ),
        SetupStep.FINISH: (
            "Review Diagnostics and recovery guidance. Setup progress is saved, and failed steps can "
            "be resumed without editing files or opening a terminal."
        ),
    }
