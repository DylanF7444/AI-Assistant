from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant.storage.database import SettingsStore

ONBOARDING_SETTINGS_KEY = "onboarding_state_v2"


class SetupStep(StrEnum):
    PRIVACY = "privacy"
    HARDWARE = "hardware"
    MODELS = "models"
    AUDIO = "audio"
    ACTIVATION = "activation"
    PERMISSIONS = "permissions"
    TYPED_TEST = "typed_test"
    VOICE_TEST = "voice_test"
    SAFETY = "safety"
    FINISH = "finish"


SETUP_STEPS = tuple(SetupStep)


class OnboardingState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[2] = 2
    status: Literal["not_started", "in_progress", "completed", "completed_degraded"] = "not_started"
    current_step: SetupStep = SetupStep.PRIVACY
    completed_steps: tuple[SetupStep, ...] = ()
    skipped_steps: dict[SetupStep, str] = Field(default_factory=dict)
    failed_step: SetupStep | None = None
    last_error_code: str | None = Field(default=None, max_length=100)
    started_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


class RecoveryIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z0-9_.-]+$", max_length=100)
    severity: Literal["info", "warning", "error"]
    component: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=500)
    settings_area: str | None = Field(default=None, max_length=80)


class OnboardingStore:
    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings

    def get(self) -> OnboardingState:
        value = self.settings.get(ONBOARDING_SETTINGS_KEY, {})
        try:
            return OnboardingState.model_validate(value)
        except Exception:
            return OnboardingState()

    def start(self) -> OnboardingState:
        current = self.get()
        if current.status in {"completed", "completed_degraded"}:
            return current
        now = datetime.now(UTC)
        state = current.model_copy(
            update={
                "status": "in_progress",
                "started_at": current.started_at or now,
                "updated_at": now,
            }
        )
        return self._set(state)

    def reset(self) -> OnboardingState:
        self.settings.set("first_run_complete", False)
        return self._set(OnboardingState())

    def complete_step(self, step: SetupStep) -> OnboardingState:
        current = self.start()
        completed = tuple(dict.fromkeys((*current.completed_steps, step)))
        next_step = next((item for item in SETUP_STEPS if item not in completed), SetupStep.FINISH)
        state = current.model_copy(
            update={
                "completed_steps": completed,
                "current_step": next_step,
                "failed_step": None,
                "last_error_code": None,
                "updated_at": datetime.now(UTC),
            }
        )
        return self._set(state)

    def skip_step(self, step: SetupStep, reason: str) -> OnboardingState:
        reason = reason.strip()
        if not reason:
            raise ValueError("A skipped setup step requires a reason")
        current = self.start()
        skipped = {**current.skipped_steps, step: reason[:500]}
        next_step = next(
            (
                item
                for item in SETUP_STEPS
                if item not in current.completed_steps and item not in skipped
            ),
            SetupStep.FINISH,
        )
        return self._set(
            current.model_copy(
                update={
                    "skipped_steps": skipped,
                    "current_step": next_step,
                    "updated_at": datetime.now(UTC),
                }
            )
        )

    def fail_step(self, step: SetupStep, error_code: str) -> OnboardingState:
        current = self.start()
        return self._set(
            current.model_copy(
                update={
                    "current_step": step,
                    "failed_step": step,
                    "last_error_code": error_code[:100],
                    "updated_at": datetime.now(UTC),
                }
            )
        )

    def set_current_step(self, step: SetupStep) -> OnboardingState:
        current = self.start()
        return self._set(
            current.model_copy(update={"current_step": step, "updated_at": datetime.now(UTC)})
        )

    def finish(self) -> OnboardingState:
        current = self.start()
        handled = set(current.completed_steps) | set(current.skipped_steps)
        missing = set(SETUP_STEPS[:-1]) - handled
        if missing:
            raise ValueError(
                "Setup cannot finish until every step is completed or explicitly skipped: "
                + ", ".join(sorted(item.value for item in missing))
            )
        now = datetime.now(UTC)
        state = current.model_copy(
            update={
                "status": "completed_degraded" if current.skipped_steps else "completed",
                "current_step": SetupStep.FINISH,
                "completed_steps": tuple(
                    dict.fromkeys((*current.completed_steps, SetupStep.FINISH))
                ),
                "completed_at": now,
                "updated_at": now,
                "failed_step": None,
                "last_error_code": None,
            }
        )
        self.settings.set("first_run_complete", True)
        return self._set(state)

    def _set(self, state: OnboardingState) -> OnboardingState:
        self.settings.set(ONBOARDING_SETTINGS_KEY, state.model_dump(mode="json"))
        return state


def recovery_issues(
    hardware: dict,
    provider_health: dict[str, dict] | None = None,
    *,
    database_integrity: str | bool = "ok",
    requested_stt_device: str = "auto",
) -> tuple[RecoveryIssue, ...]:
    providers = provider_health or {}
    issues = []
    if hardware.get("audio_devices", 0) == 0:
        issues.append(
            RecoveryIssue(
                code="audio.no_devices",
                severity="error",
                component="Audio",
                title="No audio devices were detected",
                action="Connect or enable a microphone and speaker, then refresh Diagnostics.",
                settings_area="Settings",
            )
        )
    if requested_stt_device == "cuda" and not hardware.get("cuda_available"):
        issues.append(
            RecoveryIssue(
                code="stt.cuda_unavailable",
                severity="error",
                component="Speech recognition",
                title="CUDA speech recognition is selected but unavailable",
                action="Install the CUDA 12 runtime or choose CPU/Auto for STT in Settings.",
                settings_area="Settings",
            )
        )
    for name, health in sorted(providers.items()):
        if health.get("healthy"):
            continue
        error = str(health.get("error") or "unavailable").casefold()
        action = (
            f"Install or select the local {name.upper()} model in Models, then run its health test."
            if "model" in error or "configured" in error or "installed" in error
            else f"Open Diagnostics for {name.upper()} details and retry its health test."
        )
        issues.append(
            RecoveryIssue(
                code=f"provider.{name}.unavailable",
                severity="warning",
                component=name.upper(),
                title=f"{name.upper()} is unavailable",
                action=action,
                settings_area="Models",
            )
        )
    database_ok = (
        database_integrity
        if isinstance(database_integrity, bool)
        else database_integrity.casefold() == "ok"
    )
    if not database_ok:
        issues.append(
            RecoveryIssue(
                code="database.integrity",
                severity="error",
                component="Local data",
                title="The local database integrity check failed",
                action="Export a diagnostic bundle and restore from a verified backup before continuing.",
                settings_area="Diagnostics",
            )
        )
    return tuple(issues)


def outcome_category(
    error: Exception | str,
) -> Literal["unsupported", "unavailable", "denied", "timed_out", "failed"]:
    name = error.__class__.__name__.casefold()
    text = str(error).casefold()
    if isinstance(error, NotImplementedError) or "unsupported" in text:
        return "unsupported"
    if isinstance(error, PermissionError) or "denied" in text or "permission" in text:
        return "denied"
    if isinstance(error, TimeoutError) or "timed out" in text or "timeout" in name:
        return "timed_out"
    if isinstance(error, (FileNotFoundError, ConnectionError)) or "unavailable" in text:
        return "unavailable"
    return "failed"


def outcome_guidance(error: Exception | str) -> tuple[str, str]:
    """Return a stable user-facing outcome label and next action."""
    category = outcome_category(error)
    return {
        "unsupported": (
            "This request is not supported",
            "Try a supported file, provider, or action, or open Skills to inspect alternatives.",
        ),
        "unavailable": (
            "A required component is unavailable",
            "Open Diagnostics to check the provider, model, file, device, or connection.",
        ),
        "denied": (
            "Access was denied",
            "Review the exact permission and target in Permissions, then retry if appropriate.",
        ),
        "timed_out": (
            "The operation timed out",
            "Check Diagnostics and retry. The assistant did not assume the action completed.",
        ),
        "failed": (
            "The operation failed",
            "Review the details below, then open Diagnostics if the problem continues.",
        ),
    }[category]
