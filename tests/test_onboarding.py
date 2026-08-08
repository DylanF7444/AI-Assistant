from __future__ import annotations

from assistant.onboarding import (
    SETUP_STEPS,
    OnboardingStore,
    SetupStep,
    outcome_category,
    outcome_guidance,
    recovery_issues,
)
from assistant.storage.database import Database, SettingsStore


def _store(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'onboarding.db'}")
    database.migrate()
    return OnboardingStore(SettingsStore(database)), SettingsStore(database)


def test_onboarding_progress_is_resumable_and_finishes_only_when_handled(tmp_path):
    store, settings = _store(tmp_path)
    started = store.start()
    progressed = store.complete_step(SetupStep.PRIVACY)
    resumed = OnboardingStore(settings).get()

    assert started.status == "in_progress"
    assert progressed.current_step == SetupStep.HARDWARE
    assert resumed == progressed

    for step in SETUP_STEPS[1:-1]:
        if step == SetupStep.VOICE_TEST:
            store.skip_step(step, "No microphone is attached to this test machine.")
        else:
            store.complete_step(step)
    finished = store.finish()

    assert finished.status == "completed_degraded"
    assert settings.get("first_run_complete") is True
    assert finished.skipped_steps[SetupStep.VOICE_TEST]


def test_onboarding_failure_retains_safe_error_code_and_reset(tmp_path):
    store, settings = _store(tmp_path)

    failed = store.fail_step(SetupStep.MODELS, "model_missing")
    reset = store.reset()

    assert failed.failed_step == SetupStep.MODELS
    assert failed.last_error_code == "model_missing"
    assert reset.status == "not_started"
    assert settings.get("first_run_complete") is False


def test_recovery_center_distinguishes_common_actionable_failures():
    issues = recovery_issues(
        {"audio_devices": 0, "cuda_available": False},
        {
            "stt": {"healthy": False, "error": "model is not installed"},
            "tts": {"healthy": True},
        },
        database_integrity="malformed",
        requested_stt_device="cuda",
    )

    codes = {issue.code for issue in issues}
    assert codes == {
        "audio.no_devices",
        "stt.cuda_unavailable",
        "provider.stt.unavailable",
        "database.integrity",
    }
    assert all(issue.action for issue in issues)


def test_known_failure_outcomes_are_not_collapsed_to_generic_error():
    assert outcome_category(NotImplementedError("unsupported format")) == "unsupported"
    assert outcome_category(FileNotFoundError("missing model")) == "unavailable"
    assert outcome_category(PermissionError("denied")) == "denied"
    assert outcome_category(TimeoutError("late")) == "timed_out"
    assert outcome_category(RuntimeError("broken")) == "failed"
    assert outcome_category("provider unavailable") == "unavailable"
    assert outcome_guidance("permission denied") == (
        "Access was denied",
        "Review the exact permission and target in Permissions, then retry if appropriate.",
    )


def test_setup_cannot_finish_early_and_navigation_is_persisted(tmp_path):
    store, settings = _store(tmp_path)

    with __import__("pytest").raises(ValueError, match="cannot finish"):
        store.finish()
    moved = store.set_current_step(SetupStep.MODELS)

    assert moved.current_step == SetupStep.MODELS
    assert OnboardingStore(settings).get().current_step == SetupStep.MODELS
