from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from assistant.contracts import PermissionScope, StrictModel


class MetricValueKind(StrEnum):
    NUMBER = "number"
    PERCENTAGE = "percentage"
    DURATION = "duration"
    BOOLEAN = "boolean"
    STATUS = "status"


class MetricPresentation(StrEnum):
    NUMBER = "number"
    PERCENTAGE = "percentage"
    DURATION = "duration"
    STATUS = "status"
    PROGRESS = "progress"
    TREND = "trend"


class MetricState(StrEnum):
    READY = "ready"
    STALE = "stale"
    PERMISSION_REQUIRED = "permission_required"
    OFFLINE = "offline"
    EMPTY = "empty"
    ERROR = "error"


class MetricDetail(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=160)


class MetricPoint(StrictModel):
    timestamp: datetime
    value: float


class MetricDefinition(StrictModel):
    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    source_label: str = Field(min_length=1, max_length=120)
    value_kind: MetricValueKind
    unit: str | None = Field(default=None, max_length=30)
    supported_presentations: tuple[MetricPresentation, ...]
    default_presentation: MetricPresentation
    required_capabilities: tuple[PermissionScope, ...] = ()
    default_refresh_seconds: int = Field(default=60, ge=15, le=3600)
    minimum_refresh_seconds: int = Field(default=15, ge=15, le=3600)
    supports_history: bool = False
    sensitive: bool = False
    requires_connection: bool = False

    @model_validator(mode="after")
    def _validate_defaults(self) -> MetricDefinition:
        if not self.supported_presentations:
            raise ValueError("Metric must support at least one presentation")
        if self.default_presentation not in self.supported_presentations:
            raise ValueError("Default presentation must be supported")
        if self.default_refresh_seconds < self.minimum_refresh_seconds:
            raise ValueError("Default refresh interval is below the provider minimum")
        return self


class DashboardCard(StrictModel):
    card_id: UUID
    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    connection_id: UUID | None = None
    title: str | None = Field(default=None, max_length=120)
    presentation: MetricPresentation
    size: Literal["small", "medium", "wide"] = "medium"
    refresh_seconds: int = Field(default=60, ge=15, le=3600)
    position: int = Field(ge=0)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    enabled: bool = True


class DashboardLayout(StrictModel):
    layout_version: Literal[1] = 1
    cards: tuple[DashboardCard, ...]

    @model_validator(mode="after")
    def _validate_cards(self) -> DashboardLayout:
        card_ids = [card.card_id for card in self.cards]
        if len(set(card_ids)) != len(card_ids):
            raise ValueError("Dashboard card IDs must be unique")
        positions = [card.position for card in self.cards]
        if len(set(positions)) != len(positions):
            raise ValueError("Dashboard card positions must be unique")
        return self


class MetricRequest(StrictModel):
    metric_id: str
    connection_id: UUID | None = None
    since: datetime
    options: dict[str, JsonValue] = Field(default_factory=dict)


class MetricSnapshot(StrictModel):
    metric_id: str
    connection_id: UUID | None = None
    collected_at: datetime
    state: MetricState
    value: float | int | bool | str | None = None
    unit: str | None = None
    summary: str | None = Field(default=None, max_length=500)
    details: tuple[MetricDetail, ...] = ()
    trend: tuple[MetricPoint, ...] = ()
    error_code: str | None = Field(default=None, max_length=100)


class DashboardPermissionRequest(StrictModel):
    card_id: UUID
    metric_id: str
    display_name: str
    subject_id: str
    scopes: tuple[PermissionScope, ...]


class ProviderConnection(StrictModel):
    connection_id: UUID = Field(default_factory=uuid4)
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    endpoint: str | None = Field(default=None, max_length=1000)
    configuration: dict[str, JsonValue] = Field(default_factory=dict)
    secret_references: dict[str, str] = Field(default_factory=dict)
    history_enabled: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProviderHealth(StrictModel):
    healthy: bool
    checked_at: datetime
    message: str | None = Field(default=None, max_length=300)
    error_code: str | None = Field(default=None, max_length=100)
