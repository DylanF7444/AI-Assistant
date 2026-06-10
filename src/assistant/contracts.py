from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.0"


def utcnow() -> datetime:
    return datetime.now(UTC)


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


class Transcript(StrictModel):
    session_id: UUID
    raw_text: str
    normalized_text: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    language: str | None = None
    words: tuple[WordTimestamp, ...] = ()
    is_final: bool = True


class Intent(StrictModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    source: Literal["deterministic", "rule", "classifier", "llm"]
    ambiguous: bool = False
    ambiguity_reasons: tuple[str, ...] = ()


class PermissionScope(StrictModel):
    capability: str
    targets: tuple[str, ...] = ()
    reason: str


class SkillManifest(StrictModel):
    skill_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    version: str
    api_version: Literal["1.0"] = "1.0"
    display_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permissions: tuple[PermissionScope, ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    entry_point: str
    supports_rollback: bool = False
    safe_to_retry: bool = False


class PermissionContext(StrictModel):
    user_id: str = "local-user"
    session_id: str
    granted_capabilities: tuple[str, ...] = ()


class SkillCall(StrictModel):
    skill_id: str
    caller: Literal["user", "router", "planner", "macro"] = "router"
    arguments: dict[str, Any] = Field(default_factory=dict)
    permission_context: PermissionContext
    correlation_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()))


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


class PipelineEvent(StrictModel):
    event_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID
    state: PipelineState
    timestamp: datetime = Field(default_factory=utcnow)
    message: str = ""
    progress: float | None = Field(default=None, ge=0, le=1)
    data: dict[str, Any] = Field(default_factory=dict)


class PlanStep(StrictModel):
    step_id: str
    call: SkillCall
    depends_on: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=30, gt=0)


class ExecutionPlan(StrictModel):
    plan_id: UUID = Field(default_factory=uuid4)
    steps: tuple[PlanStep, ...]
    timeout_seconds: float = Field(default=120, gt=0)
