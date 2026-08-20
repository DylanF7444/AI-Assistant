from __future__ import annotations

from typing import Literal
from uuid import UUID

from assistant.bootstrap import Container
from assistant.command_center import (
    BindingStatus,
    DestinationBinding,
    DestinationDefinition,
    PendingDelivery,
    SourceBinding,
    SourceDefinition,
    WorkArtifact,
)
from assistant.contracts import PermissionContext, PipelineState, Response
from assistant.dashboard.contracts import (
    DashboardLayout,
    DashboardPermissionRequest,
    MetricDefinition,
    MetricSnapshot,
    ProviderConnection,
    ProviderHealth,
)
from assistant.execution.engine import ExecutionOutcome
from assistant.integrations.connections import ConnectionProfile
from assistant.policy.permissions import GrantType
from assistant.presentation.session import MediaSessionRegistry
from assistant.scheduling.contracts import ScheduleDefinition, ScheduledTrigger


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

    async def stop_speaking(self) -> None:
        await self.container.orchestrator.stop_speaking()

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

    def command_center_source_definitions(
        self,
        *,
        configurable_only: bool = False,
    ) -> tuple[SourceDefinition, ...]:
        definitions = self.container.command_center.source_definitions()
        if not configurable_only:
            return definitions
        return tuple(
            definition
            for definition in definitions
            if self.command_center_skill_availability(definition.skill_id) != "unavailable"
        )

    def command_center_destination_definitions(
        self,
        *,
        configurable_only: bool = False,
    ) -> tuple[DestinationDefinition, ...]:
        definitions = self.container.command_center.destination_definitions()
        if not configurable_only:
            return definitions
        return tuple(
            definition
            for definition in definitions
            if self.command_center_skill_availability(definition.skill_id) != "unavailable"
        )

    def command_center_skill_availability(
        self,
        skill_id: str | None,
    ) -> Literal["enabled", "disabled", "unavailable"]:
        if skill_id is None:
            return "enabled"
        return self.container.registry.availability(skill_id)

    def command_center_extension_id(self, skill_id: str | None) -> str:
        if skill_id is None:
            return ""
        return next(
            (
                extension.extension_id
                for extension in self.container.extension_hub.catalog.extensions
                if extension.extension_id == skill_id
            ),
            "",
        )

    def command_center_source_bindings(self) -> tuple[SourceBinding, ...]:
        return self.container.command_center.source_bindings()

    def command_center_destination_bindings(self) -> tuple[DestinationBinding, ...]:
        return self.container.command_center.destination_bindings()

    def command_center_source_schema(self, source_id: str) -> dict:
        contribution = self.container.command_center.registry.source(source_id)
        return contribution.configuration_model.model_json_schema()

    def command_center_destination_schema(self, destination_id: str) -> dict:
        contribution = self.container.command_center.registry.destination(destination_id)
        return contribution.configuration_model.model_json_schema()

    def save_command_center_source(self, binding: SourceBinding) -> SourceBinding:
        return self.container.command_center.save_source(binding)

    def save_command_center_destination(
        self,
        binding: DestinationBinding,
    ) -> DestinationBinding:
        return self.container.command_center.save_destination(binding)

    def delete_command_center_source(self, binding_id: UUID) -> SourceBinding:
        return self.container.command_center.delete_source(binding_id)

    def delete_command_center_destination(self, binding_id: UUID) -> DestinationBinding:
        return self.container.command_center.delete_destination(binding_id)

    def command_center_source_status(self, binding: SourceBinding) -> BindingStatus:
        return self.container.command_center.source_status(binding)

    def command_center_destination_status(self, binding: DestinationBinding) -> BindingStatus:
        return self.container.command_center.destination_status(binding)

    def connection_profiles(
        self,
        provider_id: str | None = None,
    ) -> tuple[ConnectionProfile, ...]:
        return self.container.connection_profiles.list(provider_id)

    def command_center_artifacts(self) -> tuple[WorkArtifact, ...]:
        return self.container.command_center.work_artifacts()

    def edit_command_center_artifact(
        self,
        artifact_id: UUID,
        *,
        expected_revision: int,
        title: str,
        body: str,
    ) -> WorkArtifact:
        return self.container.command_center.edit_artifact(
            artifact_id,
            expected_revision=expected_revision,
            title=title,
            body=body,
        )

    def delete_command_center_artifact(self, artifact_id: UUID) -> WorkArtifact:
        return self.container.command_center.delete_artifact(artifact_id)

    def clear_command_center_artifact_data(self) -> tuple[int, int]:
        return self.container.command_center.clear_artifact_data()

    def clear_command_center_observation_data(self) -> int:
        return self.container.command_center.clear_observation_data()

    def command_center_pending_deliveries(self) -> tuple[PendingDelivery, ...]:
        return self.container.command_center.pending_deliveries()

    def schedule_command_center_brief(
        self,
        *,
        name: str,
        trigger: ScheduledTrigger,
        title: str = "Work Brief",
        retention_days: int = 30,
        destination_binding_id: UUID | None = None,
    ) -> ScheduleDefinition:
        schedule = self.container.command_center_authoring.create_command_center_brief(
            command_center=self.container.command_center,
            name=name,
            trigger=trigger,
            title=title,
            retention_days=retention_days,
            destination_binding_id=destination_binding_id,
        )
        self.container.scheduler.wake()
        return schedule

    def queue_command_center_delivery(
        self,
        artifact_id: UUID,
        destination_binding_id: UUID,
    ) -> PendingDelivery:
        return self.container.command_center.queue_delivery(
            artifact_id,
            destination_binding_id,
        )

    async def prepare_command_center_delivery(
        self,
        delivery_id: UUID,
        *,
        session_id: str = "desktop-command-center",
    ) -> tuple[PendingDelivery, ExecutionOutcome]:
        return await self.container.command_center.prepare_delivery(
            delivery_id,
            PermissionContext(session_id=session_id),
        )

    async def confirm_command_center_delivery(
        self,
        delivery_id: UUID,
        confirmation_id: UUID,
        *,
        typed_token: str | None = None,
        session_id: str = "desktop-command-center",
    ):
        return await self.container.command_center.confirm_delivery(
            delivery_id,
            confirmation_id,
            PermissionContext(session_id=session_id),
            typed_token=typed_token,
        )

    def finalize_command_center_delivery(
        self,
        delivery_id: UUID,
        outcome: ExecutionOutcome,
    ) -> PendingDelivery:
        return self.container.command_center.finalize_delivery_outcome(delivery_id, outcome)

    def cancel_command_center_delivery(
        self,
        delivery_id: UUID,
        *,
        confirmation_id: UUID | None = None,
    ) -> PendingDelivery:
        return self.container.command_center.cancel_delivery(
            delivery_id,
            confirmation_id=confirmation_id,
        )
