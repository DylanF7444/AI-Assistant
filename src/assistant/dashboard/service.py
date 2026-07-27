from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from assistant.contracts import PipelineState
from assistant.dashboard.builtins import AssistantMetricsProvider
from assistant.dashboard.contracts import (
    DashboardCard,
    DashboardLayout,
    DashboardPermissionRequest,
    MetricDefinition,
    MetricDetail,
    MetricRequest,
    MetricSnapshot,
    MetricState,
    ProviderConnection,
    ProviderHealth,
)
from assistant.dashboard.registry import DashboardProviderRegistry
from assistant.dashboard.storage import (
    DashboardConnectionStore,
    DashboardLayoutStore,
    DashboardSampleStore,
)
from assistant.policy.permissions import GrantType, PermissionManager
from assistant.storage.database import AuditStore


class DashboardService:
    def __init__(
        self,
        registry: DashboardProviderRegistry,
        layouts: DashboardLayoutStore,
        *,
        permissions: PermissionManager | None = None,
        connections: DashboardConnectionStore | None = None,
        samples: DashboardSampleStore | None = None,
        audit: AuditStore | None = None,
        user_id: str = "local-user",
        session_id: str = "desktop",
        offline_lock: bool = False,
        timeout_seconds: float = 10,
        max_concurrency: int = 4,
        max_external_concurrency: int = 2,
        history_retention_days: int = 7,
    ) -> None:
        self.registry = registry
        self.layouts = layouts
        self.permissions = permissions
        self.connection_store = connections
        self.sample_store = samples
        self.audit = audit
        self.user_id = user_id
        self.session_id = session_id
        self.offline_lock = offline_lock
        self.timeout_seconds = timeout_seconds
        self.max_concurrency = max(1, max_concurrency)
        self.max_external_concurrency = max(1, max_external_concurrency)
        self.history_retention_days = max(1, min(history_retention_days, 90))
        self._last_good: dict[tuple[str, str, str], MetricSnapshot] = {}
        self._block_categories: dict[tuple[str, str, str], str] = {}
        self._diagnostic_counts: Counter[str] = Counter()

    def layout(self) -> DashboardLayout:
        return self.layouts.load()

    def catalog(self) -> tuple[MetricDefinition, ...]:
        return self.registry.definitions()

    def default_layout(self) -> DashboardLayout:
        return self.layouts.default()

    def validate_layout(self, layout: DashboardLayout) -> DashboardLayout:
        validated = DashboardLayout.model_validate(layout)
        for card in validated.cards:
            provider, definition = self.registry.resolve(card.metric_id)
            if card.presentation not in definition.supported_presentations:
                raise ValueError(
                    f"{definition.display_name} does not support {card.presentation.value}"
                )
            if card.refresh_seconds < definition.minimum_refresh_seconds:
                raise ValueError(
                    f"{definition.display_name} cannot refresh more often than "
                    f"{definition.minimum_refresh_seconds} seconds"
                )
            connection = self._connection_for_card(card, definition)
            provider.validate_card(card, connection)
        normalized = validated.model_copy(
            update={
                "cards": tuple(
                    card.model_copy(update={"position": position})
                    for position, card in enumerate(
                        sorted(validated.cards, key=lambda item: item.position)
                    )
                )
            }
        )
        return normalized

    def save_layout(self, layout: DashboardLayout) -> DashboardLayout:
        normalized = self.validate_layout(layout)
        self.layouts.save(normalized)
        self._audit_layout("dashboard_layout_saved", normalized)
        return normalized

    def reset_layout(self) -> DashboardLayout:
        layout = self.layouts.reset()
        self._audit_layout("dashboard_layout_reset", layout)
        return layout

    def connections(self) -> tuple[ProviderConnection, ...]:
        return self.connection_store.list() if self.connection_store is not None else ()

    def set_offline_lock(self, enabled: bool) -> None:
        self.offline_lock = bool(enabled)

    def set_history_retention_days(self, days: int) -> None:
        self.history_retention_days = max(1, min(int(days), 90))

    async def create_connection(
        self,
        *,
        provider_id: str,
        display_name: str,
        endpoint: str,
        secret_values: dict[str, str],
        configuration: dict | None = None,
        history_enabled: bool = False,
    ) -> ProviderConnection:
        if self.connection_store is None:
            raise RuntimeError("Dashboard connection storage is unavailable")
        provider = self.registry.provider(provider_id)
        connection_id = uuid4()
        connection = ProviderConnection(
            connection_id=connection_id,
            provider_id=provider_id,
            display_name=" ".join(display_name.split()),
            endpoint=endpoint.strip(),
            configuration=dict(configuration or {}),
            secret_references={name: f"dashboard-{connection_id}-{name}" for name in secret_values},
            history_enabled=history_enabled,
        )
        validate_connection = getattr(provider, "validate_connection", None)
        if validate_connection is not None:
            validate_connection(connection)
        self.connection_store.save(connection, secret_values=secret_values)
        try:
            health = await self.test_connection(connection.connection_id)
            if not health.healthy:
                raise ConnectionError(health.message or "Connection health check failed")
        except Exception:
            self.connection_store.delete(connection.connection_id)
            raise
        self._audit_connection("dashboard_connection_added", connection, "enabled")
        return connection

    async def test_connection(self, connection_id: UUID) -> ProviderHealth:
        connection = self._connection(connection_id)
        provider = self.registry.provider(connection.provider_id)
        if provider.remote and self.offline_lock:
            return ProviderHealth(
                healthy=False,
                checked_at=datetime.now(UTC),
                message="The global offline lock is enabled.",
                error_code="offline",
            )
        return await asyncio.wait_for(
            provider.health(connection),
            timeout=self.timeout_seconds,
        )

    def set_connection_enabled(
        self,
        connection_id: UUID,
        enabled: bool,
    ) -> ProviderConnection:
        if self.connection_store is None:
            raise RuntimeError("Dashboard connection storage is unavailable")
        connection = self._connection(connection_id).model_copy(update={"enabled": bool(enabled)})
        saved = self.connection_store.save(connection)
        self._audit_connection(
            "dashboard_connection_changed",
            saved,
            "enabled" if enabled else "disabled",
        )
        return saved

    def set_connection_history_enabled(
        self,
        connection_id: UUID,
        enabled: bool,
    ) -> ProviderConnection:
        if self.connection_store is None:
            raise RuntimeError("Dashboard connection storage is unavailable")
        connection = self._connection(connection_id).model_copy(
            update={"history_enabled": bool(enabled)}
        )
        saved = self.connection_store.save(connection)
        self._audit_connection(
            "dashboard_connection_history_changed",
            saved,
            "enabled" if enabled else "disabled",
        )
        return saved

    def clear_connection_history(self, connection_id: UUID) -> int:
        connection = self._connection(connection_id)
        deleted = (
            self.sample_store.clear_connection(connection_id)
            if self.sample_store is not None
            else 0
        )
        self._audit_connection(
            "dashboard_connection_history_cleared",
            connection,
            f"deleted:{deleted}",
        )
        return deleted

    def delete_connection(self, connection_id: UUID) -> ProviderConnection:
        if self.connection_store is None:
            raise RuntimeError("Dashboard connection storage is unavailable")
        connection = self._connection(connection_id)
        provider = self.registry.provider(connection.provider_id)
        remaining_cards = tuple(
            card for card in self.layout().cards if card.connection_id != connection_id
        )
        self.save_layout(
            DashboardLayout(
                cards=tuple(
                    card.model_copy(update={"position": position})
                    for position, card in enumerate(remaining_cards)
                )
            )
        )
        deleted = self.connection_store.delete(connection_id)
        if self.sample_store is not None:
            self.sample_store.clear_connection(connection_id)
        if self.permissions is not None:
            subject = provider.permission_subject_for(connection)
            capabilities = {
                grant["capability"]
                for grant in self.permissions.list_grants()
                if grant["user_id"] == self.user_id and grant["skill_id"] == subject
            }
            for capability in capabilities:
                self.permissions.revoke(
                    self.user_id,
                    subject,
                    capability,
                    str(uuid4()),
                )
        self._audit_connection("dashboard_connection_removed", deleted, "deleted")
        return deleted

    def permission_request(self, card_id: UUID) -> DashboardPermissionRequest:
        card = self._card(card_id)
        provider, definition = self.registry.resolve(card.metric_id)
        connection = self._connection_for_card(card, definition)
        scopes = provider.required_capabilities(
            definition,
            connection,
            card.options,
        )
        if not scopes:
            raise ValueError(f"{definition.display_name} does not require permission")
        return DashboardPermissionRequest(
            card_id=card.card_id,
            metric_id=card.metric_id,
            display_name=definition.display_name,
            subject_id=provider.permission_subject_for(connection),
            scopes=scopes,
        )

    def grant_card_permissions(self, card_id: UUID, grant_type: GrantType) -> None:
        if self.permissions is None:
            raise RuntimeError("Dashboard permission service is unavailable")
        request = self.permission_request(card_id)
        for scope in request.scopes:
            self.permissions.grant(
                user_id=self.user_id,
                skill_id=request.subject_id,
                capability=scope.capability,
                grant_type=grant_type,
                targets=scope.targets,
                session_id=self.session_id if grant_type == GrantType.SESSION else None,
                correlation_id=str(uuid4()),
            )

    def set_pipeline_state(self, state: PipelineState) -> None:
        provider, _definition = self.registry.resolve("assistant.status")
        if isinstance(provider, AssistantMetricsProvider):
            provider.set_pipeline_state(state)

    async def refresh(
        self,
        *,
        card_ids: tuple[UUID, ...] | None = None,
    ) -> dict[UUID, MetricSnapshot]:
        selected = set(card_ids or ())
        select_all = card_ids is None
        cards = tuple(
            card
            for card in sorted(self.layout().cards, key=lambda item: item.position)
            if card.enabled and (select_all or card.card_id in selected)
        )
        local_semaphore = asyncio.Semaphore(self.max_concurrency)
        external_semaphore = asyncio.Semaphore(self.max_external_concurrency)
        tasks: dict[tuple[str, str, str], asyncio.Task[MetricSnapshot]] = {}
        for card in cards:
            key = self._cache_key(card)
            if key not in tasks:
                tasks[key] = asyncio.create_task(
                    self._collect(card, local_semaphore, external_semaphore)
                )
        if tasks:
            await asyncio.gather(*tasks.values())
        return {card.card_id: tasks[self._cache_key(card)].result() for card in cards}

    async def _collect(
        self,
        card: DashboardCard,
        local_semaphore: asyncio.Semaphore,
        external_semaphore: asyncio.Semaphore,
    ) -> MetricSnapshot:
        now = datetime.now(UTC)
        key = self._cache_key(card)
        self._diagnostic_counts["refresh_started"] += 1
        try:
            provider, definition = self.registry.resolve(card.metric_id)
            connection = self._connection_for_card(card, definition)
            provider.validate_card(card, connection)
            if provider.remote and self.offline_lock:
                self._diagnostic_counts["offline"] += 1
                self._audit_refresh_block(
                    card,
                    key,
                    provider.provider_id,
                    "offline",
                )
                return self._offline_snapshot(card, key)
            required_capabilities = provider.required_capabilities(
                definition,
                connection,
                card.options,
            )
            if required_capabilities:
                if self.permissions is None:
                    self._diagnostic_counts["permission_required"] += 1
                    return self._permission_snapshot(
                        card.metric_id,
                        required_capabilities,
                        "Permission service is unavailable.",
                    )
                decision = self.permissions.authorize_scopes(
                    user_id=self.user_id,
                    subject_id=provider.permission_subject_for(connection),
                    session_id=self.session_id,
                    required=required_capabilities,
                    consume_once=True,
                )
                if not decision.allowed:
                    self._diagnostic_counts["permission_required"] += 1
                    reason = (
                        decision.reason or "Permission is required before this metric can load."
                    )
                    return self._permission_snapshot(
                        card.metric_id,
                        required_capabilities,
                        reason,
                    )
            request = MetricRequest(
                metric_id=card.metric_id,
                connection_id=card.connection_id,
                since=now - timedelta(hours=24),
                options=card.options,
            )
            semaphore = external_semaphore if provider.remote else local_semaphore
            async with semaphore:
                snapshot = await asyncio.wait_for(
                    provider.collect(request, connection),
                    timeout=self.timeout_seconds,
                )
            if snapshot.state == MetricState.READY:
                self._block_categories.pop(key, None)
                snapshot = self._record_and_attach_history(
                    card,
                    definition,
                    connection,
                    snapshot,
                )
                self._last_good[key] = snapshot
                self._diagnostic_counts["ready"] += 1
            return snapshot
        except TimeoutError as error:
            self._diagnostic_counts["timeout"] += 1
            return self._failure_snapshot(card, key, now, error)
        except Exception as error:
            self._diagnostic_counts["error"] += 1
            return self._failure_snapshot(card, key, now, error)

    def _failure_snapshot(
        self,
        card: DashboardCard,
        key: tuple[str, str, str],
        now: datetime,
        error: Exception,
    ) -> MetricSnapshot:
        previous = self._last_good.get(key)
        if previous is not None:
            self._diagnostic_counts["stale"] += 1
            return previous.model_copy(
                update={
                    "state": MetricState.STALE,
                    "summary": "Refresh failed. Showing the last refreshed value.",
                    "error_code": type(error).__name__.lower(),
                }
            )
        return MetricSnapshot(
            metric_id=card.metric_id,
            connection_id=card.connection_id,
            collected_at=now,
            state=MetricState.ERROR,
            value=None,
            summary="Metric could not be refreshed.",
            error_code=type(error).__name__.lower(),
        )

    def _record_and_attach_history(
        self,
        card: DashboardCard,
        definition: MetricDefinition,
        connection: ProviderConnection | None,
        snapshot: MetricSnapshot,
    ) -> MetricSnapshot:
        value = snapshot.value
        if (
            self.sample_store is None
            or connection is None
            or not connection.history_enabled
            or not definition.supports_history
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            return snapshot
        cutoff = datetime.now(UTC) - timedelta(days=self.history_retention_days)
        series_key = self._series_key(card)
        try:
            self.sample_store.record(
                timestamp=snapshot.collected_at,
                connection_id=connection.connection_id,
                metric_id=card.metric_id,
                series_key=series_key,
                value=float(value),
            )
            self.sample_store.prune(cutoff)
            return snapshot.model_copy(
                update={
                    "trend": self.sample_store.history(
                        series_key=series_key,
                        since=cutoff,
                        limit=100,
                    )
                }
            )
        except Exception:
            self._diagnostic_counts["history_error"] += 1
            return snapshot

    def _card(self, card_id: UUID) -> DashboardCard:
        try:
            return next(card for card in self.layout().cards if card.card_id == card_id)
        except StopIteration as exc:
            raise KeyError(f"Unknown dashboard card: {card_id}") from exc

    def _connection(self, connection_id: UUID) -> ProviderConnection:
        if self.connection_store is None:
            raise RuntimeError("Dashboard connection storage is unavailable")
        return self.connection_store.get(connection_id)

    def _connection_for_card(
        self,
        card: DashboardCard,
        definition: MetricDefinition,
    ) -> ProviderConnection | None:
        if card.connection_id is None:
            if definition.requires_connection:
                raise ValueError(f"{definition.display_name} requires a connection")
            return None
        connection = self._connection(card.connection_id)
        if connection.provider_id != definition.provider_id:
            raise ValueError(
                f"{definition.display_name} cannot use a {connection.provider_id} connection"
            )
        if not connection.enabled:
            raise ValueError(f"Connection is disabled: {connection.display_name}")
        return connection

    @staticmethod
    def _cache_key(card: DashboardCard) -> tuple[str, str, str]:
        return (
            card.metric_id,
            str(card.connection_id or ""),
            json.dumps(card.options, sort_keys=True, separators=(",", ":"), default=str),
        )

    @classmethod
    def _series_key(cls, card: DashboardCard) -> str:
        payload = "\0".join(cls._cache_key(card))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def diagnostics(self) -> dict[str, int]:
        values = dict(self._diagnostic_counts)
        values.update(
            {
                "cards": len(self.layout().cards),
                "connections": len(self.connections()),
                "history_samples": (
                    self.sample_store.count() if self.sample_store is not None else 0
                ),
            }
        )
        return values

    @staticmethod
    def _permission_snapshot(
        metric_id: str,
        required_capabilities,
        reason: str,
    ) -> MetricSnapshot:
        return MetricSnapshot(
            metric_id=metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.PERMISSION_REQUIRED,
            value="Permission required",
            summary=reason,
            details=tuple(
                MetricDetail(
                    label=scope.capability,
                    value=", ".join(scope.targets) or "Any target",
                )
                for scope in required_capabilities
            ),
            error_code="permission_required",
        )

    def _offline_snapshot(
        self,
        card: DashboardCard,
        key: tuple[str, str, str],
    ) -> MetricSnapshot:
        previous = self._last_good.get(key)
        if previous is not None:
            return previous.model_copy(
                update={
                    "state": MetricState.OFFLINE,
                    "summary": "Offline lock is enabled. Showing the last refreshed value.",
                    "error_code": "offline",
                }
            )
        return MetricSnapshot(
            metric_id=card.metric_id,
            connection_id=card.connection_id,
            collected_at=datetime.now(UTC),
            state=MetricState.OFFLINE,
            value="Offline",
            summary="The global offline lock is enabled.",
            error_code="offline",
        )

    def _audit_connection(
        self,
        event_type: str,
        connection: ProviderConnection,
        decision: str,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            correlation_id=str(uuid4()),
            event_type=event_type,
            skill_id=f"dashboard.{connection.provider_id}",
            decision=decision,
            details={
                "connection_id": str(connection.connection_id),
                "provider_id": connection.provider_id,
                "endpoint": connection.endpoint,
            },
        )

    def _audit_layout(self, event_type: str, layout: DashboardLayout) -> None:
        if self.audit is None:
            return
        self.audit.record(
            correlation_id=str(uuid4()),
            event_type=event_type,
            skill_id="dashboard.layout",
            decision="saved",
            details={
                "card_count": len(layout.cards),
                "metric_ids": [card.metric_id for card in layout.cards],
            },
        )

    def _audit_refresh_block(
        self,
        card: DashboardCard,
        key: tuple[str, str, str],
        provider_id: str,
        category: str,
    ) -> None:
        if self._block_categories.get(key) == category:
            return
        self._block_categories[key] = category
        if self.audit is None:
            return
        self.audit.record(
            correlation_id=str(uuid4()),
            event_type="dashboard_refresh_blocked",
            skill_id=f"dashboard.{provider_id}",
            decision=category,
            details={
                "metric_id": card.metric_id,
                "connection_id": (
                    str(card.connection_id) if card.connection_id is not None else None
                ),
            },
        )
