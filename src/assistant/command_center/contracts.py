from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from assistant.contracts import ResultStatus, StrictModel
from assistant.integrations.connections import ConnectionReference


class SourceUseMode(StrEnum):
    ON_DEMAND = "on_demand"
    BRIEFING = "briefing"
    OBSERVATION = "observation"


class RetentionPolicy(StrEnum):
    TRANSIENT = "transient"
    SAVED = "saved"


class DeliveryMode(StrEnum):
    LOCAL = "local"
    DRAFT = "draft"
    CONFIRM_EACH_TIME = "confirm_each_time"
    APPROVAL_QUEUE = "approval_queue"


class SourceExecutorKind(StrEnum):
    SKILL = "skill"
    PROVIDER = "provider"


class DestinationExecutorKind(StrEnum):
    LOCAL = "local"
    SKILL = "skill"


class BindingAvailability(StrEnum):
    AVAILABLE = "available"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    CONNECTION_REQUIRED = "connection_required"
    PROFILE_DISABLED = "profile_disabled"
    INVALID = "invalid"


class DeliveryState(StrEnum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class SourceDefinition(StrictModel):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    executor_kind: SourceExecutorKind = SourceExecutorKind.SKILL
    skill_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]+$")
    operation: str | None = Field(default=None, min_length=1, max_length=80)
    provider_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]+$",
    )
    supported_modes: tuple[SourceUseMode, ...] = (SourceUseMode.ON_DEMAND,)
    requires_connection: bool = False
    remote: bool = False
    sensitive: bool = False
    background_eligible: bool = False
    observation_events: tuple[Literal["changed"], ...] = ()
    observation_category: Literal["calendar", "mail"] | None = None
    observation_minimum_interval_seconds: int = Field(default=300, ge=15, le=86_400)
    max_items: int = Field(default=50, ge=1, le=200)
    max_characters: int = Field(default=50_000, ge=1_000, le=500_000)

    @model_validator(mode="after")
    def _executor_fields_are_consistent(self) -> SourceDefinition:
        if len(set(self.supported_modes)) != len(self.supported_modes):
            raise ValueError("Source modes must be unique")
        if not self.supported_modes:
            raise ValueError("Source must support at least one use mode")
        if SourceUseMode.OBSERVATION in self.supported_modes and not self.observation_events:
            raise ValueError("Observation-capable sources must declare reviewed events")
        if SourceUseMode.OBSERVATION in self.supported_modes and self.observation_category is None:
            raise ValueError("Observation-capable sources must declare a safe category")
        if self.observation_events and SourceUseMode.OBSERVATION not in self.supported_modes:
            raise ValueError("Observation events require observation mode support")
        if self.observation_category and SourceUseMode.OBSERVATION not in self.supported_modes:
            raise ValueError("Observation category requires observation mode support")
        if self.executor_kind == SourceExecutorKind.SKILL:
            if not self.skill_id:
                raise ValueError("Skill-backed sources require skill_id")
            if self.provider_key is not None:
                raise ValueError("Skill-backed sources cannot declare provider_key")
        elif not self.provider_key:
            raise ValueError("Provider-backed sources require provider_key")
        elif self.skill_id is not None or self.operation is not None:
            raise ValueError("Provider-backed sources cannot declare skill fields")
        return self


class DestinationDefinition(StrictModel):
    destination_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    executor_kind: DestinationExecutorKind
    skill_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]+$")
    operation: str | None = Field(default=None, min_length=1, max_length=80)
    local_sink: Literal["assistant", "inbox", "notification", "export"] | None = None
    supported_delivery_modes: tuple[DeliveryMode, ...]
    requires_connection: bool = False
    remote: bool = False
    accepts_sensitive: bool = False

    @model_validator(mode="after")
    def _executor_fields_are_consistent(self) -> DestinationDefinition:
        if not self.supported_delivery_modes:
            raise ValueError("Destination must support at least one delivery mode")
        if len(set(self.supported_delivery_modes)) != len(self.supported_delivery_modes):
            raise ValueError("Destination delivery modes must be unique")
        if self.executor_kind == DestinationExecutorKind.SKILL:
            if not self.skill_id or not self.operation:
                raise ValueError("Skill-backed destinations require skill_id and operation")
            if self.local_sink is not None:
                raise ValueError("Skill-backed destinations cannot declare local_sink")
        elif not self.local_sink:
            raise ValueError("Local destinations require local_sink")
        elif self.skill_id is not None or self.operation is not None:
            raise ValueError("Local destinations cannot declare skill fields")
        return self


class SourceBinding(StrictModel):
    binding_id: UUID = Field(default_factory=uuid4)
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    connection: ConnectionReference | None = None
    configuration: dict[str, JsonValue] = Field(default_factory=dict)
    use_modes: tuple[SourceUseMode, ...] = (SourceUseMode.ON_DEMAND,)
    max_items: int = Field(default=20, ge=1, le=200)
    max_characters: int = Field(default=20_000, ge=1_000, le=500_000)
    retention: RetentionPolicy = RetentionPolicy.TRANSIENT
    minimum_freshness_seconds: int | None = Field(default=None, ge=15, le=86_400)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _use_modes_are_valid(self) -> SourceBinding:
        if not self.use_modes:
            raise ValueError("Source binding must enable at least one use mode")
        if len(set(self.use_modes)) != len(self.use_modes):
            raise ValueError("Source binding use modes must be unique")
        return self


class DestinationBinding(StrictModel):
    binding_id: UUID = Field(default_factory=uuid4)
    destination_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    connection: ConnectionReference | None = None
    configuration: dict[str, JsonValue] = Field(default_factory=dict)
    delivery_mode: DeliveryMode
    allow_sensitive: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DeliveryPayload(StrictModel):
    title: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=100_000)


class BindingStatus(StrictModel):
    binding_id: UUID
    availability: BindingAvailability
    message: str = Field(default="", max_length=300)


class SourceResultItem(StrictModel):
    item_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, min_length=1, max_length=2_000)
    kind: str = Field(default="generic", min_length=1, max_length=80)
    sensitive: bool = False


class SourceResult(StrictModel):
    binding_id: UUID
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    status: ResultStatus
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float = Field(default=0, ge=0)
    summary: str = Field(default="", max_length=1_000)
    items: tuple[SourceResultItem, ...] = ()
    error: str | None = Field(default=None, min_length=1, max_length=1_000)
    required_capabilities: tuple[str, ...] = ()
    sensitive: bool = False
    truncated: bool = False


class WorkBrief(StrictModel):
    brief_id: UUID = Field(default_factory=uuid4)
    title: str = Field(default="Work Brief", min_length=1, max_length=120)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: ResultStatus
    content: str = Field(min_length=1, max_length=200_000)
    sources: tuple[SourceResult, ...] = ()
    sensitive: bool = False


class ArtifactSourceReference(StrictModel):
    binding_id: UUID
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    collected_at: datetime


class WorkArtifact(StrictModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    revision: int = Field(default=1, ge=1)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=200_000)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    sources: tuple[ArtifactSourceReference, ...] = ()
    sensitive: bool = False
    origin_run_id: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime


class PendingDelivery(StrictModel):
    delivery_id: UUID = Field(default_factory=uuid4)
    artifact_id: UUID
    artifact_revision: int = Field(ge=1)
    artifact_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    destination_binding_id: UUID
    destination_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_preview: str = Field(min_length=1, max_length=1_000)
    state: DeliveryState = DeliveryState.PENDING
    correlation_id: UUID | None = None
    result_status: ResultStatus | None = None
    error: str | None = Field(default=None, max_length=1_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime


class ObservationCursor(StrictModel):
    schedule_id: UUID
    schedule_revision: int = Field(ge=1)
    binding_id: UUID
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    failure_count: int = Field(default=0, ge=0)
    last_error_code: str | None = Field(default=None, min_length=1, max_length=100)
    last_polled_at: datetime | None = None
    next_poll_at: datetime
    updated_at: datetime
