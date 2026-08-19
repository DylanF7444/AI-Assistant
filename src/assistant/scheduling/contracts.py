from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, TypeAdapter, field_validator, model_validator

from assistant.contracts import (
    ConfirmationRequest,
    ExecutionPlan,
    RiskLevel,
    SkillCall,
    StrictModel,
)
from assistant.logic.contracts import LogicWorkflow


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a time zone")
    return value


class ScheduleStatus(StrEnum):
    DRAFT = "draft"
    PENDING_AUTHORIZATION = "pending_authorization"
    ACTIVE = "active"
    PAUSED = "paused"
    PENDING_REVIEW = "pending_review"
    COMPLETED = "completed"
    DISABLED = "disabled"
    DELETED = "deleted"


class ScheduleRunState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    EVALUATING = "evaluating"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN_AFTER_CRASH = "unknown_after_crash"
    REJECTED = "rejected"
    EXPIRED = "expired"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


TERMINAL_RUN_STATES = frozenset(
    {
        ScheduleRunState.SUCCEEDED,
        ScheduleRunState.FAILED,
        ScheduleRunState.TIMED_OUT,
        ScheduleRunState.CANCELLED,
        ScheduleRunState.UNKNOWN_AFTER_CRASH,
        ScheduleRunState.REJECTED,
        ScheduleRunState.EXPIRED,
        ScheduleRunState.BLOCKED,
        ScheduleRunState.SKIPPED,
    }
)


class OneTimeTrigger(StrictModel):
    kind: Literal["one_time"] = "one_time"
    run_at: datetime

    @field_validator("run_at")
    @classmethod
    def _run_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "run_at")


class RecurrenceTrigger(StrictModel):
    kind: Literal["recurrence"] = "recurrence"
    frequency: Literal["minutes", "hours", "daily", "weekly", "monthly"]
    interval: int = Field(default=1, ge=1, le=10_000)
    local_time: time | None = None
    weekdays: tuple[int, ...] = ()
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    start_at: datetime
    end_at: datetime | None = None
    timezone: str
    ambiguous_time: Literal["first", "second"] = "first"
    nonexistent_time: Literal["shift_forward", "skip"] = "shift_forward"

    @field_validator("start_at", "end_at")
    @classmethod
    def _datetimes_are_aware(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)

    @field_validator("weekdays")
    @classmethod
    def _weekdays_are_valid(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("weekdays must use Monday=0 through Sunday=6")
        return tuple(sorted(set(value)))

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA time zone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _frequency_fields_are_consistent(self) -> RecurrenceTrigger:
        if self.end_at is not None and self.end_at < self.start_at:
            raise ValueError("end_at cannot be earlier than start_at")
        if self.frequency in {"daily", "weekly", "monthly"} and self.local_time is None:
            raise ValueError(f"{self.frequency} recurrence requires local_time")
        if self.frequency == "weekly" and not self.weekdays:
            raise ValueError("weekly recurrence requires at least one weekday")
        if self.frequency != "weekly" and self.weekdays:
            raise ValueError("weekdays are supported only for weekly recurrence")
        if self.frequency == "monthly" and self.day_of_month is None:
            raise ValueError("monthly recurrence requires day_of_month")
        if self.frequency != "monthly" and self.day_of_month is not None:
            raise ValueError("day_of_month is supported only for monthly recurrence")
        return self


class EventTrigger(StrictModel):
    kind: Literal["event"] = "event"
    observation_type: Literal["file", "calendar", "process", "device", "web", "integration"]
    target: str = Field(min_length=1, max_length=2_000)
    events: tuple[str, ...] = Field(min_length=1, max_length=10)
    minimum_interval_seconds: int = Field(default=30, ge=1, le=86_400)

    @field_validator("events")
    @classmethod
    def _events_are_bounded_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # The observation type is unavailable to a field validator, so the exact
        # cross-field check is performed below.
        if any(not item or len(item) > 40 for item in value):
            raise ValueError("event names must be bounded identifiers")
        return tuple(dict.fromkeys(value))

    @model_validator(mode="after")
    def _events_match_observation_type(self) -> EventTrigger:
        allowed = {
            "file": {"created", "modified", "deleted"},
            "calendar": {"starting", "created", "updated", "deleted"},
            "process": {"started", "stopped"},
            "device": {"changed", "connected", "disconnected"},
            "web": {"changed"},
            "integration": {"changed"},
        }[self.observation_type]
        unsupported = set(self.events) - allowed
        if unsupported:
            raise ValueError(
                f"unsupported {self.observation_type} event(s): {', '.join(sorted(unsupported))}"
            )
        if self.observation_type == "web":
            parsed = urlsplit(self.target)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("web event targets must use HTTPS without credentials")
            object.__setattr__(
                self,
                "target",
                urlunsplit(("https", parsed.hostname.casefold(), parsed.path or "/", "", "")),
            )
        if self.observation_type == "integration":
            try:
                object.__setattr__(self, "target", str(UUID(self.target)))
            except ValueError as exc:
                raise ValueError("integration event targets must be source binding IDs") from exc
        return self


class FileObservation(StrictModel):
    observation_type: Literal["file"] = "file"
    observation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]+$", max_length=100)
    occurred_at: datetime
    target: str = Field(min_length=1, max_length=2_000)
    event: Literal["created", "modified", "deleted"]
    size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "occurred_at")


class CalendarObservation(StrictModel):
    observation_type: Literal["calendar"] = "calendar"
    observation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]+$", max_length=100)
    occurred_at: datetime
    target: str = Field(min_length=1, max_length=2_000)
    event: Literal["starting", "created", "updated", "deleted"]
    provider: str = Field(min_length=1, max_length=80)
    event_id: str = Field(min_length=1, max_length=500)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "occurred_at")


class ProcessObservation(StrictModel):
    observation_type: Literal["process"] = "process"
    observation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]+$", max_length=100)
    occurred_at: datetime
    target: str = Field(min_length=1, max_length=2_000)
    event: Literal["started", "stopped"]
    process_id: int | None = Field(default=None, ge=0)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "occurred_at")


class DeviceObservation(StrictModel):
    observation_type: Literal["device"] = "device"
    observation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]+$", max_length=100)
    occurred_at: datetime
    target: str = Field(min_length=1, max_length=2_000)
    event: Literal["changed", "connected", "disconnected"]
    value: str | int | float | bool | None = None

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "occurred_at")


class WebObservation(StrictModel):
    observation_type: Literal["web"] = "web"
    observation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]+$", max_length=100)
    occurred_at: datetime
    target: str = Field(min_length=1, max_length=2_000)
    event: Literal["changed"] = "changed"
    content_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "occurred_at")


class IntegrationObservation(StrictModel):
    observation_type: Literal["integration"] = "integration"
    observation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]+$", max_length=100)
    occurred_at: datetime
    target: str = Field(pattern=r"^[a-fA-F0-9-]{36}$")
    event: Literal["changed"] = "changed"
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    binding_id: UUID
    safe_category: Literal["calendar", "mail"]
    content_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    resource_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "occurred_at")

    @model_validator(mode="after")
    def _target_matches_binding(self) -> IntegrationObservation:
        if self.target.casefold() != str(self.binding_id).casefold():
            raise ValueError("integration observation target must be its binding ID")
        return self


EventObservation = Annotated[
    FileObservation
    | CalendarObservation
    | ProcessObservation
    | DeviceObservation
    | WebObservation
    | IntegrationObservation,
    Field(discriminator="observation_type"),
]
OBSERVATION_ADAPTER = TypeAdapter(EventObservation)


ScheduledTrigger = Annotated[
    OneTimeTrigger | RecurrenceTrigger | EventTrigger, Field(discriminator="kind")
]
TRIGGER_ADAPTER = TypeAdapter(ScheduledTrigger)


class NotificationAction(StrictModel):
    kind: Literal["notify_only"] = "notify_only"
    title: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)


class FixedSkillAction(StrictModel):
    kind: Literal["fixed_skill"] = "fixed_skill"
    call: SkillCall


class FrozenPlanAction(StrictModel):
    kind: Literal["frozen_plan"] = "frozen_plan"
    plan: ExecutionPlan


class ScheduledBriefSource(StrictModel):
    binding_id: UUID
    binding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    call: SkillCall


class CommandCenterBriefAction(StrictModel):
    kind: Literal["command_center_brief"] = "command_center_brief"
    title: str = Field(default="Work Brief", min_length=1, max_length=120)
    sources: tuple[ScheduledBriefSource, ...] = Field(min_length=1, max_length=20)
    retention_days: int = Field(default=30, ge=1, le=365)
    destination_binding_id: UUID | None = None
    destination_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    destination_target_preview: str | None = Field(default=None, min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def _destination_snapshot_is_complete(self) -> CommandCenterBriefAction:
        values = (
            self.destination_binding_id,
            self.destination_digest,
            self.destination_target_preview,
        )
        if any(value is not None for value in values) and any(value is None for value in values):
            raise ValueError("Scheduled brief destination snapshot is incomplete")
        return self


class LogicWorkflowAction(StrictModel):
    kind: Literal["logic_workflow"] = "logic_workflow"
    workflow: LogicWorkflow


ScheduledAction = Annotated[
    NotificationAction
    | FixedSkillAction
    | FrozenPlanAction
    | CommandCenterBriefAction
    | LogicWorkflowAction,
    Field(discriminator="kind"),
]
ACTION_ADAPTER = TypeAdapter(ScheduledAction)


class AutonomyPolicy(StrictModel):
    allowed_skill_ids: tuple[str, ...] = ()
    allowed_capabilities: tuple[str, ...] = ()
    allowed_targets: tuple[str, ...] = ()
    max_risk: RiskLevel = RiskLevel.LOW
    max_steps: int = Field(default=1, ge=1, le=20)
    max_runtime_seconds: int = Field(default=30, ge=1, le=600)
    max_runs_per_hour: int = Field(default=4, ge=1, le=60)
    max_runs_per_day: int = Field(default=24, ge=1, le=1_000)
    expires_at: datetime | None = None
    requires_unlocked_session: bool = False
    allow_network: bool = False
    allow_planning: bool = False
    max_replans: int = Field(default=0, ge=0, le=2)

    @field_validator("expires_at")
    @classmethod
    def _expiry_is_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value, "expires_at")

    @model_validator(mode="after")
    def _planning_limits_are_consistent(self) -> AutonomyPolicy:
        if not self.allow_planning and self.max_replans:
            raise ValueError("max_replans requires allow_planning")
        return self


class DeliveryPolicy(StrictModel):
    on_success: Literal["always", "summary", "silent"] = "summary"
    on_failure: Literal["always"] = "always"
    on_approval_required: Literal["always"] = "always"


class RunPolicy(StrictModel):
    misfire: Literal["skip", "run_once", "catch_up"] = "run_once"
    max_lateness_seconds: int = Field(default=86_400, ge=0)
    max_catch_up_runs: int = Field(default=1, ge=0, le=10)
    concurrency: Literal["skip", "queue_one"] = "queue_one"
    max_attempts: int = Field(default=1, ge=1, le=3)
    retry_delay_seconds: int = Field(default=5, ge=1, le=3_600)
    retry_backoff: Literal["fixed", "exponential"] = "exponential"
    max_retry_delay_seconds: int = Field(default=300, ge=1, le=86_400)
    approval_window_seconds: int = Field(default=3_600, ge=30, le=604_800)
    max_consecutive_failures: int = Field(default=3, ge=1, le=100)

    @model_validator(mode="after")
    def _retry_delays_are_consistent(self) -> RunPolicy:
        if self.max_retry_delay_seconds < self.retry_delay_seconds:
            raise ValueError("max_retry_delay_seconds cannot be shorter than retry_delay_seconds")
        return self


class SkillSnapshot(StrictModel):
    skill_id: str
    version: str
    risk: RiskLevel
    permissions_digest: str
    input_schema_digest: str


class ScheduleDefinition(StrictModel):
    schedule_id: UUID = Field(default_factory=uuid4)
    user_id: str = "local-user"
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    status: ScheduleStatus = ScheduleStatus.PENDING_AUTHORIZATION
    revision: int = Field(default=1, ge=1)
    trigger: ScheduledTrigger
    action: ScheduledAction
    autonomy_policy: AutonomyPolicy = Field(default_factory=AutonomyPolicy)
    run_policy: RunPolicy = Field(default_factory=RunPolicy)
    delivery_policy: DeliveryPolicy = Field(default_factory=DeliveryPolicy)
    skill_snapshot: tuple[SkillSnapshot, ...] = ()
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    authorized_at: datetime | None = None
    paused_reason: str | None = None
    review_reason: str | None = None

    @field_validator("next_run_at", "last_run_at", "created_at", "updated_at", "authorized_at")
    @classmethod
    def _schedule_datetimes_are_aware(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)


class ScheduleRun(StrictModel):
    run_id: UUID = Field(default_factory=uuid4)
    schedule_id: UUID
    schedule_revision: int = Field(ge=1)
    occurrence_key: str
    scheduled_for: datetime
    state: ScheduleRunState = ScheduleRunState.PENDING
    attempt: int = Field(default=0, ge=0)
    lateness_seconds: float = Field(default=0, ge=0)
    lease_owner: UUID | None = None
    lease_expires_at: datetime | None = None
    correlation_id: UUID | None = None
    idempotency_key: str
    action_digest: str
    result_status: str | None = None
    result: dict | None = None
    error: dict | None = None
    approval_expires_at: datetime | None = None
    blocked_reason: str | None = None
    retry_at: datetime | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator(
        "scheduled_for",
        "lease_expires_at",
        "approval_expires_at",
        "retry_at",
        "created_at",
        "started_at",
        "completed_at",
    )
    @classmethod
    def _run_datetimes_are_aware(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)


class BackgroundAuthorization(StrictModel):
    authorization_id: UUID = Field(default_factory=uuid4)
    schedule_id: UUID
    schedule_revision: int = Field(ge=1)
    user_id: str
    machine_identity: str
    action_digest: str
    policy_digest: str
    allowed_skill_ids: tuple[str, ...]
    allowed_capabilities: tuple[str, ...]
    allowed_targets: tuple[str, ...]
    max_risk: RiskLevel
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    @field_validator("created_at", "expires_at", "revoked_at")
    @classmethod
    def _authorization_datetimes_are_aware(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)


class ApprovalConfirmation(StrictModel):
    step_id: str
    request: ConfirmationRequest


class ApprovalReview(StrictModel):
    run_id: UUID
    schedule_id: UUID
    schedule_revision: int = Field(ge=1)
    schedule_name: str
    action_digest: str
    risk: RiskLevel
    confirmations: tuple[ApprovalConfirmation, ...]
    approval_expires_at: datetime

    @field_validator("approval_expires_at")
    @classmethod
    def _approval_expiry_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "approval_expires_at")
