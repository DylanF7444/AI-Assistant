from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

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
from assistant.observability import InteractionMode, MetricStage
from assistant.scheduling.service import SchedulerService
from assistant.storage.database import Database, ItemRow, MetricsStore

ASSISTANT_PROVIDER_ID = "assistant.local"
AUTOMATION_PROVIDER_ID = "assistant.automations"
TASKS_PROVIDER_ID = "assistant.tasks"


def _card_id(name: str):
    return uuid5(NAMESPACE_URL, f"local-assistant:dashboard:{name}")


def _dashboard_task_title(title: str) -> str:
    normalized = " ".join(title.split())
    return normalized if len(normalized) <= 160 else f"{normalized[:157].rstrip()}..."


def default_dashboard_layout() -> DashboardLayout:
    metrics = (
        ("assistant.status", MetricPresentation.STATUS),
        ("assistant.request_outcomes", MetricPresentation.NUMBER),
        ("assistant.cloud_usage", MetricPresentation.NUMBER),
        ("assistant.typed_request_latency", MetricPresentation.DURATION),
        ("assistant.voice_request_latency", MetricPresentation.DURATION),
        ("assistant.model_latency", MetricPresentation.DURATION),
        ("assistant.execution_latency", MetricPresentation.DURATION),
        ("assistant.resource_usage", MetricPresentation.NUMBER),
        ("automations.summary", MetricPresentation.NUMBER),
        ("tasks.summary", MetricPresentation.NUMBER),
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
                size=(
                    "wide"
                    if metric_id in {"assistant.recent_activity", "tasks.summary"}
                    else "medium"
                ),
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
                metric_id="assistant.cloud_usage",
                provider_id=self.provider_id,
                display_name="Cloud usage",
                description=(
                    "Token usage and estimated cost for OpenAI, Anthropic, and Codex requests. "
                    "Cost is shown only when pricing is configured."
                ),
                source_label="Local Assistant",
                value_kind=MetricValueKind.NUMBER,
                unit="tokens",
                supported_presentations=(MetricPresentation.NUMBER,),
                default_presentation=MetricPresentation.NUMBER,
                supports_history=True,
            ),
            MetricDefinition(
                metric_id="assistant.request_latency",
                provider_id=self.provider_id,
                display_name="Request latency",
                description="Combined post-transcript request duration from the last 24 hours.",
                source_label="Local Assistant",
                value_kind=MetricValueKind.DURATION,
                unit="ms",
                supported_presentations=(MetricPresentation.DURATION,),
                default_presentation=MetricPresentation.DURATION,
                supports_history=True,
            ),
            MetricDefinition(
                metric_id="assistant.typed_request_latency",
                provider_id=self.provider_id,
                display_name="Typed request latency",
                description="Typed request duration from the last 24 hours.",
                source_label="Local Assistant",
                value_kind=MetricValueKind.DURATION,
                unit="ms",
                supported_presentations=(MetricPresentation.DURATION,),
                default_presentation=MetricPresentation.DURATION,
                supports_history=True,
            ),
            MetricDefinition(
                metric_id="assistant.voice_request_latency",
                provider_id=self.provider_id,
                display_name="Voice request latency",
                description="Post-transcript voice request and playback duration.",
                source_label="Local Assistant",
                value_kind=MetricValueKind.DURATION,
                unit="ms",
                supported_presentations=(MetricPresentation.DURATION,),
                default_presentation=MetricPresentation.DURATION,
                supports_history=True,
            ),
            MetricDefinition(
                metric_id="assistant.model_latency",
                provider_id=self.provider_id,
                display_name="Model latency",
                description="Total Ollama completion duration from the last 24 hours.",
                source_label="Local Assistant",
                value_kind=MetricValueKind.DURATION,
                unit="ms",
                supported_presentations=(MetricPresentation.DURATION,),
                default_presentation=MetricPresentation.DURATION,
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
                metric_id="assistant.resource_usage",
                provider_id=self.provider_id,
                display_name="Assistant resources",
                description="Latest process memory and active request queue samples.",
                source_label="Local Assistant",
                value_kind=MetricValueKind.NUMBER,
                unit="MB",
                supported_presentations=(MetricPresentation.NUMBER,),
                default_presentation=MetricPresentation.NUMBER,
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
        if request.metric_id == "assistant.cloud_usage":
            return self._cloud_usage(request)
        if request.metric_id == "assistant.request_latency":
            return self._latency(
                request,
                stage="request.total",
                empty_summary="No request samples in the last 24 hours.",
            )
        if request.metric_id == "assistant.typed_request_latency":
            return self._latency(
                request,
                stage="request.total",
                empty_summary="No typed request samples in the last 24 hours.",
                interaction=InteractionMode.TYPED,
            )
        if request.metric_id == "assistant.voice_request_latency":
            return self._latency(
                request,
                stage="request.total",
                empty_summary="No voice request samples in the last 24 hours.",
                interaction=InteractionMode.VOICE,
            )
        if request.metric_id == "assistant.model_latency":
            return self._latency(
                request,
                stage="model.total",
                empty_summary="No local model samples in the last 24 hours.",
            )
        if request.metric_id == "assistant.execution_latency":
            return self._execution_latency(request)
        if request.metric_id == "assistant.resource_usage":
            return self._resource_usage(request)
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
        counts = self.metrics.outcome_counts(since=request.since, stage="request.total")
        total = sum(counts.values())
        if not total:
            return MetricSnapshot(
                metric_id=request.metric_id,
                collected_at=datetime.now(UTC),
                state=MetricState.EMPTY,
                value=0,
                summary="No request samples in the last 24 hours.",
            )
        errors = counts.get("error", 0) + counts.get("cancelled", 0)
        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=total,
            summary=f"{total - errors} handled  •  {errors} errors/cancelled",
            details=tuple(
                MetricDetail(
                    label=outcome.replace("_", " ").title(),
                    value=str(count),
                )
                for outcome, count in sorted(counts.items())
            ),
        )

    def _execution_latency(self, request: MetricRequest) -> MetricSnapshot:
        return self._latency(
            request,
            stage="execution",
            empty_summary="No execution samples in the last 24 hours.",
        )

    def _cloud_usage(self, request: MetricRequest) -> MetricSnapshot:
        token_rows = self.metrics.query(
            since=request.since,
            stages=(str(MetricStage.CLOUD_TOKENS),),
        )
        cost_rows = self.metrics.query(
            since=request.since,
            stages=(str(MetricStage.CLOUD_COST),),
        )
        token_total = sum(
            float(row["value"]) for row in token_rows if isinstance(row.get("value"), int | float)
        )
        cost_values = [
            float(row["value"]) for row in cost_rows if isinstance(row.get("value"), int | float)
        ]
        if not token_rows and not cost_rows:
            return MetricSnapshot(
                metric_id=request.metric_id,
                collected_at=datetime.now(UTC),
                state=MetricState.EMPTY,
                value=None,
                unit="tokens",
                summary="No cloud usage samples in the last 24 hours.",
            )

        by_provider: dict[tuple[str, str], dict[str, Any]] = {}
        for row in token_rows:
            dimensions = row.get("dimensions") or {}
            key = (str(dimensions.get("provider", "Unknown")), str(dimensions.get("model", "")))
            by_provider.setdefault(key, {"tokens": 0.0, "cost": 0.0, "cost_available": False})[
                "tokens"
            ] += float(row.get("value") or 0)
        for row in cost_rows:
            dimensions = row.get("dimensions") or {}
            key = (str(dimensions.get("provider", "Unknown")), str(dimensions.get("model", "")))
            values = by_provider.setdefault(
                key, {"tokens": 0.0, "cost": 0.0, "cost_available": False}
            )
            values["cost"] += float(row.get("value") or 0)
            values["cost_available"] = True
        if cost_values:
            summary = f"{int(token_total):,} tokens  •  estimated cost ${sum(cost_values):.6f}"
        else:
            summary = f"{int(token_total):,} tokens  •  cost estimate unavailable"
        details = tuple(
            MetricDetail(
                label=f"{provider} {model}".rstrip(),
                value=(
                    f"{int(values['tokens']):,} tokens"
                    + (f"  •  ${values['cost']:.6f}" if values["cost_available"] else "")
                ),
            )
            for (provider, model), values in sorted(by_provider.items())
        )
        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=int(token_total),
            unit="tokens",
            summary=summary,
            details=details,
        )

    def _resource_usage(self, request: MetricRequest) -> MetricSnapshot:
        memory = self.metrics.latest(stages=("resource.memory",))
        queue = self.metrics.latest(stages=("resource.queue_depth",))
        if memory is None:
            return MetricSnapshot(
                metric_id=request.metric_id,
                collected_at=datetime.now(UTC),
                state=MetricState.EMPTY,
                value=None,
                unit="MB",
                summary="No resource samples yet.",
            )
        memory_mb = float(memory["value"] or 0) / (1024 * 1024)
        queue_depth = int(float(queue["value"] or 0)) if queue is not None else 0
        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=round(memory_mb, 1),
            unit="MB",
            summary=f"{memory_mb:.1f} MB working set  •  {queue_depth} active requests",
            details=(
                MetricDetail(label="Working set", value=f"{memory_mb:.1f} MB"),
                MetricDetail(label="Active requests", value=str(queue_depth)),
            ),
        )

    def _latency(
        self,
        request: MetricRequest,
        *,
        stage: str,
        empty_summary: str,
        interaction: InteractionMode | None = None,
    ) -> MetricSnapshot:
        summary = self.metrics.stage_summary(
            since=request.since,
            stage=stage,
            dimensions={"interaction": interaction} if interaction is not None else None,
        )
        if not summary["count"]:
            return MetricSnapshot(
                metric_id=request.metric_id,
                collected_at=datetime.now(UTC),
                state=MetricState.EMPTY,
                value=None,
                unit="ms",
                summary=empty_summary,
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
                MetricDetail(label="Errors / cancelled", value=str(summary["failed"])),
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


class TaskMetricsProvider:
    provider_id = TASKS_PROVIDER_ID
    permission_subject = "dashboard.tasks"
    remote = False

    def __init__(self, database: Database) -> None:
        self.database = database

    def definitions(self) -> tuple[MetricDefinition, ...]:
        return (
            MetricDefinition(
                metric_id="tasks.summary",
                provider_id=self.provider_id,
                display_name="Tasks",
                description="Open and completed tasks in the local task list.",
                source_label="Local Tasks",
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
            raise ValueError("Local task metrics do not use a connection")

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
        if request.metric_id != "tasks.summary":
            raise KeyError(request.metric_id)
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ItemRow)
                    .where(ItemRow.kind == "task")
                    .order_by(ItemRow.completed, ItemRow.id)
                )
            )

        open_count = sum(not row.completed for row in rows)
        completed_count = len(rows) - open_count
        if not rows:
            return MetricSnapshot(
                metric_id=request.metric_id,
                collected_at=datetime.now(UTC),
                state=MetricState.EMPTY,
                value=0,
                summary="No tasks in the local list.",
            )

        return MetricSnapshot(
            metric_id=request.metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=open_count,
            summary=f"{open_count} open  •  {completed_count} completed",
            details=tuple(
                MetricDetail(
                    label=f"{'Open' if not row.completed else 'Done'} #{row.id}",
                    value=_dashboard_task_title(row.title),
                )
                for row in rows
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
