from __future__ import annotations

import threading
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from assistant.config import Settings
from assistant.contracts import PipelineState
from assistant.dashboard.contracts import (
    DashboardCard,
    DashboardLayout,
    MetricDefinition,
    MetricDetail,
    MetricPresentation,
    MetricRequest,
    MetricSnapshot,
    MetricState,
    MetricValueKind,
    ProviderConnection,
    ProviderHealth,
)
from assistant.scheduling.service import SchedulerService
from assistant.storage.database import MetricsStore

ASSISTANT_PROVIDER_ID = "assistant.local"
AUTOMATION_PROVIDER_ID = "assistant.automations"


def _card_id(name: str):
    return uuid5(NAMESPACE_URL, f"local-assistant:dashboard:{name}")


def default_dashboard_layout() -> DashboardLayout:
    metrics = (
        ("assistant.status", MetricPresentation.STATUS),
        ("assistant.request_outcomes", MetricPresentation.NUMBER),
        ("assistant.execution_latency", MetricPresentation.DURATION),
        ("automations.summary", MetricPresentation.NUMBER),
        ("assistant.recent_activity", MetricPresentation.STATUS),
    )
    return DashboardLayout(
        cards=tuple(
            DashboardCard(
                card_id=_card_id(metric_id),
                metric_id=metric_id,
                presentation=presentation,
                refresh_seconds=60,
                position=position,
                size="wide" if metric_id == "assistant.recent_activity" else "medium",
            )
            for position, (metric_id, presentation) in enumerate(metrics)
        )
    )


class AssistantMetricsProvider:
    provider_id = ASSISTANT_PROVIDER_ID
    permission_subject = "dashboard.assistant"
    remote = False

    def __init__(self, metrics: MetricsStore, settings: Settings) -> None:
        self.metrics = metrics
        self.settings = settings
        self._pipeline_state = PipelineState.IDLE
        self._state_lock = threading.RLock()

    def definitions(self) -> tuple[MetricDefinition, ...]:
        return (
            MetricDefinition(
                metric_id="assistant.status",
                provider_id=self.provider_id,
                display_name="Assistant status",
                description="Current foreground assistant and offline state.",
                source_label="Local Assistant",
                value_kind=MetricValueKind.STATUS,
                supported_presentations=(MetricPresentation.STATUS,),
                default_presentation=MetricPresentation.STATUS,
            ),
            MetricDefinition(
                metric_id="assistant.request_outcomes",
                provider_id=self.provider_id,
                display_name="Request outcomes",
                description="Privacy-bounded execution outcomes from the last 24 hours.",
                source_label="Local Assistant",
                value_kind=MetricValueKind.NUMBER,
                supported_presentations=(MetricPresentation.NUMBER,),
                default_presentation=MetricPresentation.NUMBER,
                supports_history=True,
            ),
            MetricDefinition(
                metric_id="assistant.execution_latency",
                provider_id=self.provider_id,
                display_name="Execution latency",
                description="Average and percentile execution duration from the last 24 hours.",
                source_label="Local Assistant",
                value_kind=MetricValueKind.DURATION,
                unit="ms",
                supported_presentations=(MetricPresentation.DURATION,),
                default_presentation=MetricPresentation.DURATION,
                supports_history=True,
            ),
            MetricDefinition(
                metric_id="assistant.recent_activity",
                provider_id=self.provider_id,
                display_name="Recent activity",
                description="Most recent privacy-bounded execution or automation outcome.",
                source_label="Local Assistant",
                value_kind=MetricValueKind.STATUS,
                supported_presentations=(MetricPresentation.STATUS,),
                default_presentation=MetricPresentation.STATUS,
            ),
        )

    def set_pipeline_state(self, state: PipelineState) -> None:
        with self._state_lock:
            self._pipeline_state = state

    def validate_card(
        self,
        _card: DashboardCard,
        connection: ProviderConnection | None,
    ) -> None:
        if connection is not None:
            raise ValueError("Local assistant metrics do not use a connection")

    @staticmethod
    def required_capabilities(
        definition: MetricDefinition,
        _connection: ProviderConnection | None,
        _options: dict,
    ):
        return definition.required_capabilities

    def permission_subject_for(self, _connection: ProviderConnection | None) -> str:
        return self.permission_subject

    async def health(self, _connection: ProviderConnection | None = None) -> ProviderHealth:
        return ProviderHealth(healthy=True, checked_at=datetime.now(UTC), message="Ready")

    async def collect(
        self,
        request: MetricRequest,
        _connection: ProviderConnection | None = None,
    ) -> MetricSnapshot:
        if request.metric_id == "assistant.status":
            return self._status(request)
        if request.metric_id == "assistant.request_outcomes":
            return self._request_outcomes(request)
        if request.metric_id == "assistant.execution_latency":
            return self._execution_latency(request)
        if request.metric_id == "assistant.recent_activity":
            return self._recent_activity(request)
        raise KeyError(request.metric_id)

    def _status(self, request: MetricRequest) -> MetricSnapshot:
        with self._state_lock:
            state = self._pipeline_state
        label = state.value.replace("_", " ").title()
        if state == PipelineState.IDLE:
            label = "Ready"
        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=label,
            summary=(
                f"Offline lock {'on' if self.settings.offline_lock else 'off'}"
                f"  •  Model {self.settings.llm_model}"
            ),
            details=(MetricDetail(label="Voice mode", value=self.settings.voice_activation_mode),),
        )

    def _request_outcomes(self, request: MetricRequest) -> MetricSnapshot:
        counts = self.metrics.outcome_counts(since=request.since, stage="execution")
        total = sum(counts.values())
        if not total:
            return MetricSnapshot(
                metric_id=request.metric_id,
                collected_at=datetime.now(UTC),
                state=MetricState.EMPTY,
                value=0,
                summary="No execution samples in the last 24 hours.",
            )
        completed = counts.get("completed", 0)
        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=total,
            summary=f"{completed} completed  •  {total - completed} other",
            details=tuple(
                MetricDetail(
                    label=outcome.replace("_", " ").title(),
                    value=str(count),
                )
                for outcome, count in sorted(counts.items())
            ),
        )

    def _execution_latency(self, request: MetricRequest) -> MetricSnapshot:
        summary = self.metrics.duration_summary(since=request.since, stage="execution")
        if not summary["count"]:
            return MetricSnapshot(
                metric_id=request.metric_id,
                collected_at=datetime.now(UTC),
                state=MetricState.EMPTY,
                value=None,
                unit="ms",
                summary="No execution samples in the last 24 hours.",
            )
        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=round(summary["average"], 1),
            unit="ms",
            summary=f"p95 {summary['p95']:.1f} ms  •  {summary['count']} samples",
            details=(
                MetricDetail(label="Median", value=f"{summary['p50']:.1f} ms"),
                MetricDetail(label="95th percentile", value=f"{summary['p95']:.1f} ms"),
            ),
        )

    def _recent_activity(self, request: MetricRequest) -> MetricSnapshot:
        latest = self.metrics.latest(stages=("execution", "background_schedule"))
        if latest is None:
            return MetricSnapshot(
                metric_id=request.metric_id,
                collected_at=datetime.now(UTC),
                state=MetricState.EMPTY,
                value="No activity yet",
                summary="Completed requests and automations will appear here.",
            )
        timestamp = latest["timestamp"]
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=(latest["outcome"] or "Recorded").replace("_", " ").title(),
            summary=(
                f"{latest['stage'].replace('_', ' ').title()}"
                f"  •  {timestamp.astimezone().strftime('%b %d at %H:%M')}"
            ),
        )


class AutomationMetricsProvider:
    provider_id = AUTOMATION_PROVIDER_ID
    permission_subject = "dashboard.automations"
    remote = False

    def __init__(self, scheduler: SchedulerService, metrics: MetricsStore) -> None:
        self.scheduler = scheduler
        self.metrics = metrics

    def definitions(self) -> tuple[MetricDefinition, ...]:
        return (
            MetricDefinition(
                metric_id="automations.summary",
                provider_id=self.provider_id,
                display_name="Automations",
                description="Active schedules, queued runs, and recent background outcomes.",
                source_label="Local Automations",
                value_kind=MetricValueKind.NUMBER,
                supported_presentations=(MetricPresentation.NUMBER,),
                default_presentation=MetricPresentation.NUMBER,
            ),
        )

    def validate_card(
        self,
        _card: DashboardCard,
        connection: ProviderConnection | None,
    ) -> None:
        if connection is not None:
            raise ValueError("Automation metrics do not use a connection")

    @staticmethod
    def required_capabilities(
        definition: MetricDefinition,
        _connection: ProviderConnection | None,
        _options: dict,
    ):
        return definition.required_capabilities

    def permission_subject_for(self, _connection: ProviderConnection | None) -> str:
        return self.permission_subject

    async def health(self, _connection: ProviderConnection | None = None) -> ProviderHealth:
        return ProviderHealth(healthy=True, checked_at=datetime.now(UTC), message="Ready")

    async def collect(
        self,
        request: MetricRequest,
        _connection: ProviderConnection | None = None,
    ) -> MetricSnapshot:
        if request.metric_id != "automations.summary":
            raise KeyError(request.metric_id)
        health = await self.scheduler.health()
        outcomes = self.metrics.outcome_counts(
            since=request.since,
            stage="background_schedule",
        )
        active = int(health["active_schedules"])
        queued = int(health["queued_runs"])
        paused = bool(health["paused"])
        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=active,
            summary=(
                f"{queued} queued or awaiting approval  •  {'Paused' if paused else 'Running'}"
            ),
            details=(
                MetricDetail(label="Recent completed", value=str(outcomes.get("completed", 0))),
                MetricDetail(
                    label="Recent failed",
                    value=str(outcomes.get("failed", 0) + outcomes.get("timed_out", 0)),
                ),
            ),
        )
