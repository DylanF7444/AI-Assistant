from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "1.0"


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_confidence(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and 1 < value <= 100:
        return value / 100
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["1.0"] = CONTRACT_VERSION


class PipelineState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    ERROR = "error"
    CANCELLED = "cancelled"


class TracePhase(StrEnum):
    UNDERSTANDING = "understanding"
    DECISION = "decision"
    PERMISSION = "permission"
    ACTION = "action"
    RESULT = "result"


class BackgroundMode(StrEnum):
    SAFE = "safe"
    CAPABILITY_GATED = "capability_gated"
    FOREGROUND_ONLY = "foreground_only"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ResultStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partially_completed"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ResponseType(StrEnum):
    ANSWER = "answer"
    SKILL = "skill"
    CLARIFICATION = "clarification"
    PERMISSION = "permission"
    CONFIRMATION = "confirmation"
    CONTROL = "control"
    STATUS = "status"
    ERROR = "error"


class MediaKind(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class MediaSourceType(StrEnum):
    LOCAL_FILE = "local_file"
    REMOTE_CACHE = "remote_cache"


class MediaAction(StrEnum):
    SHOW = "show"
    PLAY = "play"
    PAUSE = "pause"
    TOGGLE_PLAYBACK = "toggle_playback"
    STOP = "stop"
    SEEK_RELATIVE = "seek_relative"
    SEEK_ABSOLUTE = "seek_absolute"
    SET_VOLUME = "set_volume"
    SET_RATE = "set_rate"
    TOGGLE_MUTE = "toggle_mute"
    FULLSCREEN = "fullscreen"
    EXIT_FULLSCREEN = "exit_fullscreen"
    CLOSE = "close"


class MediaSource(StrictModel):
    source_type: MediaSourceType
    location: str = Field(min_length=1, max_length=32_768)
    origin: str | None = Field(default=None, min_length=1, max_length=2_000)
    kind: MediaKind
    mime_type: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    size_bytes: int = Field(ge=0)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(default=None, ge=0)

    @field_validator("location", "origin", "title", "mime_type")
    @classmethod
    def _media_text_must_not_contain_controls(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(character) < 32 for character in value):
            raise ValueError("Media source text cannot contain control characters")
        return value

    @model_validator(mode="after")
    def _remote_cache_requires_sanitized_provenance(self) -> MediaSource:
        if self.source_type == MediaSourceType.REMOTE_CACHE:
            if self.origin is None:
                raise ValueError("Remote cache media requires sanitized provenance")
            parsed = urlparse(self.origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Remote media provenance must be a sanitized HTTPS URL")
        elif self.origin is not None:
            raise ValueError("Local media cannot have remote provenance")
        return self


class MediaDirective(StrictModel):
    directive_id: UUID = Field(default_factory=uuid4)
    presentation_id: UUID
    action: MediaAction
    source: MediaSource | None = None
    autoplay: bool = False
    position_seconds: float | None = Field(default=None, ge=0)
    delta_seconds: float | None = None
    volume: float | None = Field(default=None, ge=0, le=1)
    playback_rate: float | None = Field(default=None, ge=0.25, le=4)

    @model_validator(mode="after")
    def _fields_must_match_media_action(self) -> MediaDirective:
        required_field = {
            MediaAction.SEEK_RELATIVE: "delta_seconds",
            MediaAction.SEEK_ABSOLUTE: "position_seconds",
            MediaAction.SET_VOLUME: "volume",
            MediaAction.SET_RATE: "playback_rate",
        }.get(self.action)
        values = {
            "position_seconds": self.position_seconds,
            "delta_seconds": self.delta_seconds,
            "volume": self.volume,
            "playback_rate": self.playback_rate,
        }
        if self.action == MediaAction.SHOW:
            if self.source is None:
                raise ValueError("Show directives require a media source")
        elif self.source is not None:
            raise ValueError("Only show directives may contain a media source")
        if self.action != MediaAction.SHOW and self.autoplay:
            raise ValueError("Only show directives may request autoplay")
        if required_field is not None and values[required_field] is None:
            raise ValueError(f"{self.action.value} requires {required_field}")
        if self.action == MediaAction.SEEK_RELATIVE and self.delta_seconds == 0:
            raise ValueError("Relative seek delta cannot be zero")
        for field_name, value in values.items():
            if (
                value is not None
                and field_name != required_field
                and not (self.action == MediaAction.SHOW and field_name == "position_seconds")
            ):
                raise ValueError(f"{field_name} is not valid for {self.action.value}")
        return self


class AudioSession(StrictModel):
    session_id: UUID = Field(default_factory=uuid4)
    source: str
    sample_format: str = "pcm_s16le"
    sample_rate: int = Field(default=16000, gt=0)
    channels: int = Field(default=1, ge=1, le=2)
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None


class WordTimestamp(StrictModel):
    word: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value: Any) -> Any:
        return normalize_confidence(value)


class Transcript(StrictModel):
    session_id: UUID
    raw_text: str
    normalized_text: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    language: str | None = None
    words: tuple[WordTimestamp, ...] = ()
    is_final: bool = True

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value: Any) -> Any:
        return normalize_confidence(value)


class Intent(StrictModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    source: Literal["deterministic", "rule", "classifier", "llm"]
    ambiguous: bool = False
    ambiguity_reasons: tuple[str, ...] = ()

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value: Any) -> Any:
        return normalize_confidence(value)


class PermissionScope(StrictModel):
    capability: str
    targets: tuple[str, ...] = ()
    reason: str


class SkillManifest(StrictModel):
    skill_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    version: str
    api_version: Literal["1.0"] = "1.0"
    display_name: str
    category: str = Field(default="General", min_length=1, max_length=80)
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permissions: tuple[PermissionScope, ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    entry_point: str
    supports_rollback: bool = False
    safe_to_retry: bool = False
    background_mode: BackgroundMode = BackgroundMode.FOREGROUND_ONLY


class PermissionContext(StrictModel):
    user_id: str = "local-user"
    session_id: str
    granted_capabilities: tuple[str, ...] = ()
    invocation: Literal["interactive", "background"] = "interactive"
    schedule_id: UUID | None = None
    schedule_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _background_context_requires_schedule(self) -> PermissionContext:
        has_schedule = self.schedule_id is not None or self.schedule_revision is not None
        if self.invocation == "background" and (
            self.schedule_id is None or self.schedule_revision is None
        ):
            raise ValueError("Background permission context requires a schedule and revision")
        if self.invocation == "interactive" and has_schedule:
            raise ValueError("Interactive permission context cannot name a schedule")
        return self


class SkillCall(StrictModel):
    skill_id: str
    caller: Literal["user", "router", "planner", "macro", "scheduler"] = "router"
    arguments: dict[str, Any] = Field(default_factory=dict)
    permission_context: PermissionContext
    correlation_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()))

    @model_validator(mode="after")
    def _scheduler_calls_require_background_context(self) -> SkillCall:
        if self.caller == "scheduler" and self.permission_context.invocation != "background":
            raise ValueError("Scheduler calls require a background permission context")
        if self.permission_context.invocation == "background" and self.caller != "scheduler":
            raise ValueError("Background calls must identify the scheduler as caller")
        return self


class SkillError(StrictModel):
    code: str
    message: str
    recoverable: bool = False


class SkillResult(StrictModel):
    status: ResultStatus
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: SkillError | None = None
    rollback_token: str | None = None
    display_text: str | None = None
    speech_text: str | None = None
    media_directive: MediaDirective | None = None
    sensitive: bool = False


class ConfirmationRequest(StrictModel):
    confirmation_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID
    action_digest: str
    skill_id: str
    exact_action: str
    exact_targets: tuple[str, ...]
    risk: RiskLevel
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    requires_typed_token: bool = False


class Response(StrictModel):
    display_text: str
    speech_text: str | None = None
    response_type: ResponseType = ResponseType.STATUS
    severity: Literal["info", "success", "warning", "error"] = "info"
    speak: bool = False
    sensitive: bool = False
    updates_display: bool = True
    media_directive: MediaDirective | None = None


class ActivityTrace(StrictModel):
    phase: TracePhase
    label: str = Field(min_length=1, max_length=80)
    detail: str = Field(default="", max_length=500)
    invocation: Literal["interactive", "background"] = "interactive"
    skill_id: str | None = None


class PipelineEvent(StrictModel):
    event_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID
    state: PipelineState
    timestamp: datetime = Field(default_factory=utcnow)
    message: str = ""
    progress: float | None = Field(default=None, ge=0, le=1)
    data: dict[str, Any] = Field(default_factory=dict)
    trace: ActivityTrace | None = None
    updates_foreground_status: bool = True


class PlanStep(StrictModel):
    step_id: str
    call: SkillCall
    depends_on: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=30, gt=0)


class ExecutionPlan(StrictModel):
    plan_id: UUID = Field(default_factory=uuid4)
    steps: tuple[PlanStep, ...]
    timeout_seconds: float = Field(default=120, gt=0)
