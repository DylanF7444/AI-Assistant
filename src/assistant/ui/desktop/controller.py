from __future__ import annotations

from uuid import UUID

from assistant.bootstrap import Container
from assistant.contracts import PipelineState, Response
from assistant.dashboard.contracts import (
    DashboardLayout,
    DashboardPermissionRequest,
    MetricDefinition,
    MetricSnapshot,
    ProviderConnection,
    ProviderHealth,
)
from assistant.execution.engine import ExecutionOutcome
from assistant.policy.permissions import GrantType
from assistant.presentation.session import MediaSessionRegistry


class DesktopController:
    """Typed boundary between Qt widgets and application services."""

    def __init__(self, container: Container) -> None:
        self.container = container

    async def run_text(self, text: str) -> tuple[Response, ExecutionOutcome]:
        return await self.container.orchestrator.handle_text(text)

    def has_replanning_continuation(self, outcome: ExecutionOutcome) -> bool:
        return self.container.orchestrator.has_replanning_continuation(outcome)

    def discard_replanning_continuation(self, outcome: ExecutionOutcome) -> None:
        self.container.orchestrator.discard_replanning_continuation(outcome)

    async def continue_replanning(
        self,
        outcome: ExecutionOutcome,
    ) -> tuple[Response, ExecutionOutcome]:
        return await self.container.orchestrator.continue_replanning(outcome)

    async def confirm_replanning(
        self,
        outcome: ExecutionOutcome,
        *,
        typed_token: str | None = None,
    ) -> tuple[Response, ExecutionOutcome]:
        return await self.container.orchestrator.confirm_replanning(
            outcome,
            typed_token=typed_token,
        )

    async def cancel(self) -> None:
        await self.container.audio_capture.stop()
        await self.container.stt.cancel()
        await self.container.orchestrator.cancel()

    @property
    def media_session_registry(self) -> MediaSessionRegistry:
        return self.container.media_sessions

    def dashboard_layout(self) -> DashboardLayout:
        return self.container.dashboard.layout()

    def dashboard_catalog(self) -> tuple[MetricDefinition, ...]:
        return self.container.dashboard.catalog()

    def dashboard_default_layout(self) -> DashboardLayout:
        return self.container.dashboard.default_layout()

    def validate_dashboard_layout(self, layout: DashboardLayout) -> DashboardLayout:
        return self.container.dashboard.validate_layout(layout)

    def save_dashboard_layout(self, layout: DashboardLayout) -> DashboardLayout:
        return self.container.dashboard.save_layout(layout)

    def reset_dashboard_layout(self) -> DashboardLayout:
        return self.container.dashboard.reset_layout()

    def dashboard_permission_request(self, card_id: UUID) -> DashboardPermissionRequest:
        return self.container.dashboard.permission_request(card_id)

    def grant_dashboard_permission(self, card_id: UUID, grant_type: GrantType) -> None:
        self.container.dashboard.grant_card_permissions(card_id, grant_type)

    def dashboard_connections(self) -> tuple[ProviderConnection, ...]:
        return self.container.dashboard.connections()

    async def add_home_assistant_connection(
        self,
        *,
        display_name: str,
        endpoint: str,
        token: str,
        history_enabled: bool = False,
    ) -> ProviderConnection:
        return await self.container.dashboard.create_connection(
            provider_id="home_assistant",
            display_name=display_name,
            endpoint=endpoint,
            secret_values={"token": token},
            history_enabled=history_enabled,
        )

    async def test_dashboard_connection(self, connection_id: UUID) -> ProviderHealth:
        return await self.container.dashboard.test_connection(connection_id)

    def set_dashboard_connection_enabled(
        self,
        connection_id: UUID,
        enabled: bool,
    ) -> ProviderConnection:
        return self.container.dashboard.set_connection_enabled(connection_id, enabled)

    def set_dashboard_connection_history_enabled(
        self,
        connection_id: UUID,
        enabled: bool,
    ) -> ProviderConnection:
        return self.container.dashboard.set_connection_history_enabled(
            connection_id,
            enabled,
        )

    def clear_dashboard_connection_history(self, connection_id: UUID) -> int:
        return self.container.dashboard.clear_connection_history(connection_id)

    def delete_dashboard_connection(self, connection_id: UUID) -> ProviderConnection:
        return self.container.dashboard.delete_connection(connection_id)

    async def refresh_dashboard(
        self,
        card_ids: tuple[UUID, ...] | None = None,
    ) -> dict[UUID, MetricSnapshot]:
        return await self.container.dashboard.refresh(card_ids=card_ids)

    def set_dashboard_pipeline_state(self, state: PipelineState) -> None:
        self.container.dashboard.set_pipeline_state(state)
