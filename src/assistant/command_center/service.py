from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID

from assistant.command_center.artifacts import (
    CommandCenterArtifactStore,
    destination_digest,
    source_binding_digest,
)
from assistant.command_center.contracts import (
    BindingAvailability,
    BindingStatus,
    DeliveryPayload,
    DeliveryState,
    DestinationBinding,
    DestinationDefinition,
    DestinationExecutorKind,
    ObservationCursor,
    PendingDelivery,
    SourceBinding,
    SourceDefinition,
    SourceExecutorKind,
    SourceResult,
    SourceResultItem,
    SourceUseMode,
    WorkArtifact,
    WorkBrief,
)
from assistant.command_center.observations import CommandCenterObservationStore
from assistant.command_center.registry import CommandCenterRegistry
from assistant.command_center.storage import CommandCenterBindingStore
from assistant.contracts import (
    PermissionContext,
    PlanStep,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.execution.engine import ExecutionEngine, ExecutionOutcome
from assistant.scheduling.notifications import NotificationCenter, NotificationMessage
from assistant.skills.registry import SkillRegistry
from assistant.storage.connections import ConnectionProfileStore
from assistant.storage.database import AuditStore


class CommandCenterService:
    def __init__(
        self,
        registry: CommandCenterRegistry,
        bindings: CommandCenterBindingStore,
        skills: SkillRegistry,
        connections: ConnectionProfileStore,
        engine: ExecutionEngine | None = None,
        notifications: NotificationCenter | None = None,
        artifacts: CommandCenterArtifactStore | None = None,
        audit: AuditStore | None = None,
        observations: CommandCenterObservationStore | None = None,
    ) -> None:
        self.registry = registry
        self.bindings = bindings
        self.skills = skills
        self.connections = connections
        self.engine = engine
        self.notifications = notifications
        self.artifacts = artifacts
        self.audit = audit
        self.observations = observations

    def source_definitions(self) -> tuple[SourceDefinition, ...]:
        return self.registry.source_definitions()

    def destination_definitions(self) -> tuple[DestinationDefinition, ...]:
        return self.registry.destination_definitions()

    def source_bindings(self) -> tuple[SourceBinding, ...]:
        return self.bindings.list_sources()

    def destination_bindings(self) -> tuple[DestinationBinding, ...]:
        return self.bindings.list_destinations()

    def save_source(self, binding: SourceBinding) -> SourceBinding:
        validated = SourceBinding.model_validate(binding)
        definition = self.registry.source(validated.source_id).definition
        self._validate_source_binding(validated, definition)
        self.registry.validate_source_configuration(validated)
        if definition.executor_kind == SourceExecutorKind.SKILL:
            self.registry.source_arguments(validated, self.skills)
        saved = self.bindings.save_source(validated)
        if self.observations is not None and (
            not saved.enabled or SourceUseMode.OBSERVATION not in saved.use_modes
        ):
            self.observations.clear_binding(saved.binding_id)
        return saved

    def save_destination(self, binding: DestinationBinding) -> DestinationBinding:
        validated = DestinationBinding.model_validate(binding)
        definition = self.registry.destination(validated.destination_id).definition
        self._validate_destination_binding(validated, definition)
        self.registry.validate_destination_configuration(validated)
        if definition.executor_kind == DestinationExecutorKind.SKILL:
            self.registry.validate_destination_backing(validated, self.skills)
        return self.bindings.save_destination(validated)

    def delete_source(self, binding_id: UUID) -> SourceBinding:
        deleted = self.bindings.delete_source(binding_id)
        if self.observations is not None:
            self.observations.clear_binding(binding_id)
        return deleted

    def delete_destination(self, binding_id: UUID) -> DestinationBinding:
        return self.bindings.delete_destination(binding_id)

    def source_status(self, binding: SourceBinding) -> BindingStatus:
        try:
            definition = self.registry.source(binding.source_id).definition
            self._validate_source_binding(binding, definition)
            if not binding.enabled:
                return BindingStatus(
                    binding_id=binding.binding_id,
                    availability=BindingAvailability.DISABLED,
                    message="Source binding is disabled.",
                )
            if definition.executor_kind == SourceExecutorKind.SKILL:
                assert definition.skill_id is not None
                availability = self.skills.availability(definition.skill_id)
                if availability != "enabled":
                    state = (
                        BindingAvailability.DISABLED
                        if availability == "disabled"
                        else BindingAvailability.UNAVAILABLE
                    )
                    return BindingStatus(
                        binding_id=binding.binding_id,
                        availability=state,
                        message=f"Backing skill is {availability}.",
                    )
            connection_status = self._connection_status(binding.connection)
            if connection_status is not None:
                return BindingStatus(
                    binding_id=binding.binding_id,
                    availability=connection_status,
                    message="Connection profile is disabled or unavailable.",
                )
            return BindingStatus(
                binding_id=binding.binding_id,
                availability=BindingAvailability.AVAILABLE,
            )
        except (KeyError, TypeError, ValueError):
            return BindingStatus(
                binding_id=binding.binding_id,
                availability=BindingAvailability.INVALID,
                message="Source binding is invalid.",
            )

    def destination_status(self, binding: DestinationBinding) -> BindingStatus:
        try:
            definition = self.registry.destination(binding.destination_id).definition
            self._validate_destination_binding(binding, definition)
            if not binding.enabled:
                return BindingStatus(
                    binding_id=binding.binding_id,
                    availability=BindingAvailability.DISABLED,
                    message="Destination binding is disabled.",
                )
            if definition.executor_kind == DestinationExecutorKind.SKILL:
                assert definition.skill_id is not None
                availability = self.skills.availability(definition.skill_id)
                if availability != "enabled":
                    state = (
                        BindingAvailability.DISABLED
                        if availability == "disabled"
                        else BindingAvailability.UNAVAILABLE
                    )
                    return BindingStatus(
                        binding_id=binding.binding_id,
                        availability=state,
                        message=f"Backing skill is {availability}.",
                    )
            connection_status = self._connection_status(binding.connection)
            if connection_status is not None:
                return BindingStatus(
                    binding_id=binding.binding_id,
                    availability=connection_status,
                    message="Connection profile is disabled or unavailable.",
                )
            return BindingStatus(
                binding_id=binding.binding_id,
                availability=BindingAvailability.AVAILABLE,
            )
        except (KeyError, TypeError, ValueError):
            return BindingStatus(
                binding_id=binding.binding_id,
                availability=BindingAvailability.INVALID,
                message="Destination binding is invalid.",
            )

    def compile_source_call(
        self,
        binding: SourceBinding,
        permission_context: PermissionContext,
    ) -> SkillCall:
        definition = self.registry.source(binding.source_id).definition
        self._validate_source_binding(binding, definition)
        if definition.executor_kind != SourceExecutorKind.SKILL:
            raise ValueError("Provider-backed source does not compile to a skill call")
        if self.source_status(binding).availability != BindingAvailability.AVAILABLE:
            raise PermissionError("Source binding is not available")
        assert definition.skill_id is not None
        arguments = self.registry.source_arguments(binding, self.skills)
        return SkillCall(
            skill_id=definition.skill_id,
            caller=("scheduler" if permission_context.invocation == "background" else "user"),
            arguments=arguments,
            permission_context=permission_context,
            retain_audit_content=False,
        )

    def compile_destination_call(
        self,
        binding: DestinationBinding,
        permission_context: PermissionContext,
        payload: DeliveryPayload | None = None,
    ) -> SkillCall:
        definition = self.registry.destination(binding.destination_id).definition
        self._validate_destination_binding(binding, definition)
        if definition.executor_kind != DestinationExecutorKind.SKILL:
            raise ValueError("Local destination does not compile to a skill call")
        if self.destination_status(binding).availability != BindingAvailability.AVAILABLE:
            raise PermissionError("Destination binding is not available")
        assert definition.skill_id is not None
        arguments = self.registry.destination_arguments(binding, self.skills, payload)
        return SkillCall(
            skill_id=definition.skill_id,
            caller="user",
            arguments=arguments,
            permission_context=permission_context,
        )

    def _validate_source_binding(
        self,
        binding: SourceBinding,
        definition: SourceDefinition,
    ) -> None:
        if set(binding.use_modes).difference(definition.supported_modes):
            raise ValueError("Source binding enables an unsupported use mode")
        if binding.max_items > definition.max_items:
            raise ValueError("Source binding item limit exceeds the definition maximum")
        if binding.max_characters > definition.max_characters:
            raise ValueError("Source binding character limit exceeds the definition maximum")
        if SourceUseMode.OBSERVATION in binding.use_modes:
            if not definition.observation_events or not definition.background_eligible:
                raise ValueError("Source does not support reviewed background observation")
            if (
                binding.minimum_freshness_seconds is not None
                and binding.minimum_freshness_seconds
                < definition.observation_minimum_interval_seconds
            ):
                raise ValueError("Observation interval is below the provider minimum")
        self._validate_connection(
            binding.connection, definition.provider_id, definition.requires_connection
        )

    def _validate_destination_binding(
        self,
        binding: DestinationBinding,
        definition: DestinationDefinition,
    ) -> None:
        if binding.delivery_mode not in definition.supported_delivery_modes:
            raise ValueError("Destination binding uses an unsupported delivery mode")
        if binding.allow_sensitive and not definition.accepts_sensitive:
            raise ValueError("Destination does not accept sensitive artifacts")
        self._validate_connection(
            binding.connection, definition.provider_id, definition.requires_connection
        )

    @staticmethod
    def _validate_connection(connection, provider_id: str, required: bool) -> None:
        if required and connection is None:
            raise ValueError("Binding requires a connection reference")
        if connection is not None and connection.provider_id != provider_id:
            raise ValueError("Binding connection belongs to a different provider")

    def _connection_status(self, connection) -> BindingAvailability | None:
        if connection is None or connection.profile_id is None:
            return None
        return (
            None
            if self.connections.resolve(connection) is not None
            else BindingAvailability.PROFILE_DISABLED
        )

    async def collect_sources(
        self,
        permission_context: PermissionContext,
        *,
        binding_ids: tuple[UUID, ...] | None = None,
        total_timeout_seconds: float = 45,
        source_timeout_seconds: float = 25,
    ) -> tuple[SourceResult, ...]:
        """Collect enabled briefing inputs concurrently through normal execution policy."""
        if self.engine is None:
            raise RuntimeError("Command-center execution is not configured")
        if not 1 <= source_timeout_seconds <= 30:
            raise ValueError("Source timeout must be between 1 and 30 seconds")
        if not 1 <= total_timeout_seconds <= 120:
            raise ValueError("Total timeout must be between 1 and 120 seconds")

        selected = self.briefing_source_bindings(binding_ids)

        results: list[SourceResult | None] = [None] * len(selected)
        steps: list[PlanStep | None] = []
        for index, binding in enumerate(selected):
            definition = self.registry.source(binding.source_id).definition
            try:
                call = self.compile_source_call(binding, permission_context)
                steps.append(
                    PlanStep(
                        step_id=f"source-{binding.binding_id.hex}",
                        call=call,
                        timeout_seconds=source_timeout_seconds,
                    )
                )
            except Exception as exc:
                results[index] = self._source_failure(
                    binding,
                    definition,
                    ResultStatus.DENIED,
                    str(exc),
                )
                steps.append(None)

        async def collect(index: int, binding: SourceBinding, step: PlanStep) -> None:
            try:
                results[index] = await asyncio.wait_for(
                    self._collect_step(binding, step),
                    timeout=step.timeout_seconds,
                )
            except TimeoutError:
                await self.engine.cancel(step.call.correlation_id)
                definition = self.registry.source(binding.source_id).definition
                results[index] = self._source_failure(
                    binding,
                    definition,
                    ResultStatus.TIMED_OUT,
                    "The source did not finish before its timeout.",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                definition = self.registry.source(binding.source_id).definition
                results[index] = self._source_failure(
                    binding,
                    definition,
                    ResultStatus.FAILED,
                    str(exc),
                )

        tasks = {
            asyncio.create_task(collect(index, binding, step)): (index, binding, step)
            for index, (binding, step) in enumerate(zip(selected, steps, strict=True))
            if step is not None
        }
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=total_timeout_seconds)
            for task in pending:
                index, binding, step = tasks[task]
                task.cancel()
                await self.engine.cancel(step.call.correlation_id)
                definition = self.registry.source(binding.source_id).definition
                results[index] = self._source_failure(
                    binding,
                    definition,
                    ResultStatus.TIMED_OUT,
                    "The work brief reached its total timeout.",
                )
            await asyncio.gather(*done, *pending, return_exceptions=True)
        return tuple(result for result in results if result is not None)

    def briefing_source_bindings(
        self,
        binding_ids: tuple[UUID, ...] | None = None,
    ) -> tuple[SourceBinding, ...]:
        bindings = self.source_bindings()
        if binding_ids is None:
            return tuple(
                binding
                for binding in bindings
                if binding.enabled and SourceUseMode.BRIEFING in binding.use_modes
            )
        by_id = {binding.binding_id: binding for binding in bindings}
        missing = [binding_id for binding_id in binding_ids if binding_id not in by_id]
        if missing:
            raise KeyError(f"Unknown source binding: {missing[0]}")
        selected = tuple(by_id[binding_id] for binding_id in binding_ids)
        if any(SourceUseMode.BRIEFING not in binding.use_modes for binding in selected):
            raise ValueError("Selected source is not enabled for briefings")
        return selected

    def briefing_permission_checks(
        self,
        permission_context: PermissionContext,
        binding_ids: tuple[UUID, ...] | None = None,
    ) -> tuple[tuple[SkillCall, tuple], ...]:
        checks = []
        for binding in self.briefing_source_bindings(binding_ids):
            try:
                call = self.compile_source_call(binding, permission_context)
                skill = self.skills.get(call.skill_id)
                scopes = tuple(skill.required_permissions(call))
                operation = call.arguments.get("operation")
                if (
                    skill.risk_for(call) != RiskLevel.LOW
                    or any(self._mutating_capability(scope.capability) for scope in scopes)
                    or (isinstance(operation, str) and self._mutating_operation(operation))
                ):
                    continue
                checks.append((call, scopes))
            except (KeyError, PermissionError, TypeError, ValueError):
                continue
        return tuple(checks)

    def validate_source_call_read_only(self, call: SkillCall) -> None:
        skill = self.skills.get(call.skill_id)
        risk = skill.risk_for(call)
        permissions = tuple(skill.required_permissions(call))
        forbidden = tuple(
            scope.capability for scope in permissions if self._mutating_capability(scope.capability)
        )
        operation = call.arguments.get("operation")
        mutable_operation = isinstance(operation, str) and self._mutating_operation(operation)
        if risk != RiskLevel.LOW:
            raise ValueError(f"Briefing input resolved to {risk.value} risk")
        if forbidden:
            raise ValueError(
                "Briefing input requested mutable capability: " + ", ".join(sorted(set(forbidden)))
            )
        if mutable_operation:
            raise ValueError(f"Briefing input resolved to mutable operation: {operation}")

    async def _collect_step(self, binding: SourceBinding, step: PlanStep) -> SourceResult:
        assert self.engine is not None
        definition = self.registry.source(binding.source_id).definition
        skill = self.skills.get(step.call.skill_id)
        risk = skill.risk_for(step.call)
        permissions = tuple(skill.required_permissions(step.call))
        forbidden = tuple(
            scope.capability for scope in permissions if self._mutating_capability(scope.capability)
        )
        operation = step.call.arguments.get("operation")
        mutable_operation = isinstance(operation, str) and self._mutating_operation(operation)
        if risk != RiskLevel.LOW or forbidden or mutable_operation:
            reason = (
                f"Source resolved to {risk.value} risk."
                if risk != RiskLevel.LOW
                else (
                    f"Source requested mutable capability: {', '.join(sorted(set(forbidden)))}."
                    if forbidden
                    else f"Source resolved to mutable operation: {operation}."
                )
            )
            return self._source_failure(
                binding,
                definition,
                ResultStatus.DENIED,
                reason,
            )

        started = time.perf_counter()
        outcome = await self.engine.prepare(step.call)
        duration_ms = (time.perf_counter() - started) * 1000
        if outcome.permission_requests:
            capabilities = tuple(
                sorted({request.scope.capability for request in outcome.permission_requests})
            )
            return self._source_failure(
                binding,
                definition,
                ResultStatus.DENIED,
                "Permission is required before this source can be read.",
                duration_ms=duration_ms,
                required_capabilities=capabilities,
            )
        if outcome.confirmation is not None:
            return self._source_failure(
                binding,
                definition,
                ResultStatus.DENIED,
                "A source unexpectedly required action confirmation and was not run.",
                duration_ms=duration_ms,
            )
        if outcome.result is None:
            return self._source_failure(
                binding,
                definition,
                ResultStatus.FAILED,
                "The source returned no result.",
                duration_ms=duration_ms,
            )
        return self._normalize_source_result(
            binding,
            definition,
            outcome.result,
            duration_ms=duration_ms,
        )

    def build_brief(
        self,
        sources: tuple[SourceResult, ...],
        *,
        title: str = "Work Brief",
    ) -> WorkBrief:
        successful = tuple(
            source
            for source in sources
            if source.status in {ResultStatus.COMPLETED, ResultStatus.PARTIAL}
        )
        failed = tuple(source for source in sources if source not in successful)
        if not sources:
            status = ResultStatus.DENIED
        elif successful and failed:
            status = ResultStatus.PARTIAL
        elif successful:
            status = ResultStatus.COMPLETED
        else:
            status = ResultStatus.FAILED

        generated_at = datetime.now(UTC)
        lines = [
            f"# {self._markdown_text(title)}",
            "",
            f"Generated {generated_at.astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        ]
        if not sources:
            lines.extend(
                (
                    "",
                    "No briefing inputs are configured yet.",
                    "Add an input in Connections and enable its Briefing use mode.",
                )
            )
        for source in successful:
            lines.extend(("", f"## {self._markdown_text(source.display_name)}"))
            refreshed = source.collected_at.astimezone().strftime("%H:%M %Z")
            lines.append(f"_Updated {refreshed} · {self._markdown_text(source.provider_id)}_")
            if not source.items:
                lines.append(f"- {self._markdown_text(source.summary or 'No items returned.')}")
            for item in source.items:
                line = f"- {self._markdown_text(item.title)}"
                if item.description:
                    line += f" — {self._markdown_text(item.description)}"
                lines.append(line)
            if source.truncated:
                lines.append("- _Additional items were omitted by this input's limits._")
        if failed:
            lines.extend(("", "## Inputs needing attention"))
            for source in failed:
                reason = source.error or source.summary or _status_text(source.status)
                lines.append(
                    f"- **{self._markdown_text(source.display_name)}:** "
                    f"{self._markdown_text(reason)}"
                )
        content = "\n".join(lines)
        return WorkBrief(
            title=title,
            generated_at=generated_at,
            status=status,
            content=content,
            sources=sources,
            sensitive=any(source.sensitive for source in sources),
        )

    def deliver_local(
        self,
        brief: WorkBrief,
        destination: DestinationBinding,
    ) -> str | NotificationMessage:
        definition = self.registry.destination(destination.destination_id).definition
        self._validate_destination_binding(destination, definition)
        if definition.executor_kind != DestinationExecutorKind.LOCAL:
            raise ValueError("Manual work briefs support local delivery only")
        if definition.local_sink == "assistant":
            return brief.content
        if definition.local_sink != "inbox":
            raise ValueError("Manual work briefs currently support Assistant or Inbox delivery")
        if self.notifications is None:
            raise RuntimeError("Notification inbox is not configured")
        message = NotificationMessage(
            title=brief.title,
            message=self._clean_text(brief.content, limit=500),
            severity="warning" if brief.status != ResultStatus.COMPLETED else "success",
            category="report",
        )
        self.notifications.publish(message)
        return message

    def save_brief(
        self,
        brief: WorkBrief,
        *,
        retention_days: int = 30,
        origin_run_id: UUID | None = None,
    ) -> WorkArtifact:
        store = self._artifact_store()
        existing = store.find_by_origin_run(origin_run_id) if origin_run_id else None
        artifact = store.save_brief(
            brief,
            retention_days=retention_days,
            origin_run_id=origin_run_id,
        )
        if existing is not None:
            return artifact
        self._audit_artifact("command_center_artifact_saved", artifact, "saved")
        return artifact

    def normalize_scheduled_source(
        self,
        binding_id: UUID,
        result: SkillResult,
        *,
        duration_ms: float = 0,
    ) -> SourceResult:
        binding = self.bindings.get_source(binding_id)
        definition = self.registry.source(binding.source_id).definition
        return self._normalize_source_result(
            binding,
            definition,
            result,
            duration_ms=duration_ms,
        )

    def scheduled_binding_denial(self, action) -> str | None:
        """Revalidate every binding fingerprint before a scheduled brief reads data."""
        for source in action.sources:
            try:
                binding = self.bindings.get_source(source.binding_id)
                if source_binding_digest(binding) != source.binding_digest:
                    return f"Briefing input changed: {binding.display_name}"
                status = self.source_status(binding)
                if status.availability != BindingAvailability.AVAILABLE:
                    return (
                        status.message or f"Briefing input is unavailable: {binding.display_name}"
                    )
            except (KeyError, TypeError, ValueError):
                return f"Briefing input is missing or invalid: {source.binding_id}"
        if action.destination_binding_id is not None:
            try:
                destination = self.bindings.get_destination(action.destination_binding_id)
                if destination_digest(destination) != action.destination_digest:
                    return f"Briefing output changed: {destination.display_name}"
                status = self.destination_status(destination)
                if status.availability != BindingAvailability.AVAILABLE:
                    return (
                        status.message
                        or f"Briefing output is unavailable: {destination.display_name}"
                    )
                if not destination.allow_sensitive:
                    return "Scheduled briefing output is not approved for sensitive artifacts"
            except (KeyError, TypeError, ValueError):
                return f"Briefing output is missing or invalid: {action.destination_binding_id}"
        return None

    def publish_scheduled_brief(
        self,
        brief: WorkBrief,
        artifact: WorkArtifact,
        *,
        schedule_id: UUID,
        run_id: UUID,
    ) -> NotificationMessage:
        if self.notifications is None:
            raise RuntimeError("Notification inbox is not configured")
        message = NotificationMessage(
            notification_id=run_id,
            title=brief.title,
            message=(self._clean_text(brief.content, limit=420) or "Scheduled work brief ready.")
            + f"\n\nSaved artifact: {artifact.artifact_id}",
            severity=("success" if brief.status == ResultStatus.COMPLETED else "warning"),
            category="report",
            schedule_id=schedule_id,
            run_id=run_id,
        )
        self.notifications.publish(message)
        return message

    def queue_scheduled_delivery(
        self,
        artifact: WorkArtifact,
        destination_binding_id: UUID,
    ) -> PendingDelivery:
        existing = self._artifact_store().delivery_for_artifact_destination(
            artifact.artifact_id,
            destination_binding_id,
        )
        if existing is not None:
            return existing
        return self.queue_delivery(artifact.artifact_id, destination_binding_id)

    def destination_target_preview(self, binding: DestinationBinding) -> str:
        definition = self.registry.destination(binding.destination_id).definition
        self._validate_destination_binding(binding, definition)
        return self._destination_target_preview(binding, definition)

    def work_artifacts(self) -> tuple[WorkArtifact, ...]:
        return self._artifact_store().list()

    def work_artifact(self, artifact_id: UUID, revision: int | None = None) -> WorkArtifact:
        return self._artifact_store().get(artifact_id, revision)

    def edit_artifact(
        self,
        artifact_id: UUID,
        *,
        expected_revision: int,
        title: str,
        body: str,
    ) -> WorkArtifact:
        artifact = self._artifact_store().update(
            artifact_id,
            expected_revision=expected_revision,
            title=title,
            body=body,
        )
        self._audit_artifact("command_center_artifact_revised", artifact, "saved")
        return artifact

    def delete_artifact(self, artifact_id: UUID) -> WorkArtifact:
        artifact = self._artifact_store().delete(artifact_id)
        self._audit_artifact("command_center_artifact_deleted", artifact, "deleted")
        return artifact

    def clear_artifact_data(self) -> tuple[int, int]:
        counts = self._artifact_store().clear()
        if self.audit is not None:
            self.audit.record(
                correlation_id=str(UUID(int=0)),
                event_type="command_center_artifact_data_cleared",
                decision="deleted",
                details={"artifact_count": counts[0], "delivery_count": counts[1]},
            )
        return counts

    def queue_delivery(
        self,
        artifact_id: UUID,
        destination_binding_id: UUID,
        *,
        ttl_minutes: int = 30,
    ) -> PendingDelivery:
        artifact = self.work_artifact(artifact_id)
        destination = self.bindings.get_destination(destination_binding_id)
        definition = self.registry.destination(destination.destination_id).definition
        self._validate_destination_binding(destination, definition)
        status = self.destination_status(destination)
        if status.availability != BindingAvailability.AVAILABLE:
            raise PermissionError(status.message or "Destination is unavailable")
        if artifact.sensitive and not destination.allow_sensitive:
            raise PermissionError("Destination is not approved for sensitive artifacts")
        preview = self._destination_target_preview(destination, definition)
        pending = self._artifact_store().queue_delivery(
            artifact,
            destination,
            target_preview=preview,
            ttl_minutes=ttl_minutes,
        )
        self._audit_delivery("command_center_delivery_queued", pending, "pending")
        return pending

    def pending_deliveries(self) -> tuple[PendingDelivery, ...]:
        return self._artifact_store().deliveries()

    def observation_cursors(self) -> tuple[ObservationCursor, ...]:
        if self.observations is None:
            return ()
        return self.observations.list()

    def clear_observation_data(self) -> int:
        return 0 if self.observations is None else self.observations.clear()

    async def prepare_delivery(
        self,
        delivery_id: UUID,
        permission_context: PermissionContext,
    ) -> tuple[PendingDelivery, ExecutionOutcome]:
        if self.engine is None:
            raise RuntimeError("Command-center execution is not configured")
        pending, artifact, destination, definition = self._revalidate_delivery(delivery_id)
        if definition.executor_kind == DestinationExecutorKind.LOCAL:
            outcome = await self._prepare_local_delivery(
                pending,
                artifact,
                destination,
                definition,
                permission_context,
            )
        else:
            call = self.compile_destination_call(
                destination,
                permission_context,
                DeliveryPayload(title=artifact.title, body=artifact.body),
            ).model_copy(
                update={
                    "correlation_id": pending.correlation_id or UUID(int=delivery_id.int),
                    "retain_audit_content": False,
                }
            )
            outcome = await self.engine.prepare(call)
        updated = self._record_delivery_outcome(pending, outcome)
        return updated, outcome

    async def confirm_delivery(
        self,
        delivery_id: UUID,
        confirmation_id: UUID,
        permission_context: PermissionContext,
        *,
        typed_token: str | None = None,
    ) -> tuple[PendingDelivery, SkillResult]:
        if self.engine is None:
            raise RuntimeError("Command-center execution is not configured")
        pending, artifact, destination, definition = self._revalidate_delivery(delivery_id)
        if definition.executor_kind == DestinationExecutorKind.SKILL:
            call = self.compile_destination_call(
                destination,
                permission_context,
                DeliveryPayload(title=artifact.title, body=artifact.body),
            ).model_copy(
                update={
                    "correlation_id": pending.correlation_id or UUID(int=delivery_id.int),
                    "retain_audit_content": False,
                }
            )
        else:
            call = self._local_delivery_call(
                pending,
                artifact,
                destination,
                definition,
                permission_context,
            )
            if call is None:
                raise ValueError("This local delivery does not use confirmation")
        result = await self.engine.execute(
            call,
            confirmation_id=confirmation_id,
            typed_token=typed_token,
        )
        updated = self._finish_delivery(pending, result)
        return updated, result

    def cancel_delivery(
        self,
        delivery_id: UUID,
        *,
        confirmation_id: UUID | None = None,
    ) -> PendingDelivery:
        if confirmation_id is not None and self.engine is not None:
            self.engine.confirmations.deny(confirmation_id)
        pending = self._artifact_store().update_delivery(
            delivery_id,
            DeliveryState.CANCELLED,
            result_status=ResultStatus.CANCELLED,
            error="Delivery cancelled before execution.",
        )
        self._audit_delivery("command_center_delivery_cancelled", pending, "cancelled")
        return pending

    def finalize_delivery_outcome(
        self,
        delivery_id: UUID,
        outcome: ExecutionOutcome,
    ) -> PendingDelivery:
        pending = self._artifact_store().delivery(delivery_id)
        return self._record_delivery_outcome(pending, outcome)

    async def _prepare_local_delivery(
        self,
        pending: PendingDelivery,
        artifact: WorkArtifact,
        destination: DestinationBinding,
        definition: DestinationDefinition,
        permission_context: PermissionContext,
    ) -> ExecutionOutcome:
        assert self.engine is not None
        if definition.local_sink == "assistant":
            return ExecutionOutcome(
                result=SkillResult(
                    status=ResultStatus.COMPLETED,
                    summary="Artifact is ready in the Assistant.",
                    display_text=artifact.body,
                    sensitive=artifact.sensitive,
                )
            )
        if definition.local_sink == "inbox":
            if self.notifications is None:
                raise RuntimeError("Notification inbox is not configured")
            self.notifications.publish(
                NotificationMessage(
                    title=artifact.title,
                    message=self._clean_text(artifact.body, limit=500) or "Saved work artifact.",
                    severity="success",
                    category="report",
                )
            )
            return ExecutionOutcome(
                result=SkillResult(
                    status=ResultStatus.COMPLETED,
                    summary="Artifact copied to Inbox.",
                    data={"notification_created": True},
                    sensitive=artifact.sensitive,
                )
            )
        if definition.local_sink == "notification":
            call = self._local_delivery_call(
                pending,
                artifact,
                destination,
                definition,
                permission_context,
            )
            assert call is not None
            return await self.engine.prepare(call)
        if definition.local_sink == "export":
            call = self._local_delivery_call(
                pending,
                artifact,
                destination,
                definition,
                permission_context,
            )
            assert call is not None
            return await self.engine.prepare(call)
        raise ValueError("Unsupported local destination")

    def _local_delivery_call(
        self,
        pending: PendingDelivery,
        artifact: WorkArtifact,
        destination: DestinationBinding,
        definition: DestinationDefinition,
        permission_context: PermissionContext,
    ) -> SkillCall | None:
        if definition.local_sink == "notification":
            return SkillCall(
                skill_id="windows.notification",
                caller="user",
                arguments={
                    "title": str(destination.configuration.get("title") or artifact.title)[:100],
                    "message": self._clean_text(artifact.body, limit=500),
                },
                permission_context=permission_context,
                correlation_id=pending.correlation_id or UUID(int=pending.delivery_id.int),
                retain_audit_content=False,
            )
        if definition.local_sink == "export":
            return SkillCall(
                skill_id="windows.file_operation",
                caller="user",
                arguments={
                    "operation": "create_text",
                    "source": str(destination.configuration["path"]),
                    "content": artifact.body,
                },
                permission_context=permission_context,
                correlation_id=pending.correlation_id or UUID(int=pending.delivery_id.int),
                retain_audit_content=False,
            )
        return None

    def _revalidate_delivery(
        self,
        delivery_id: UUID,
    ) -> tuple[PendingDelivery, WorkArtifact, DestinationBinding, DestinationDefinition]:
        store = self._artifact_store()
        pending = store.delivery(delivery_id)
        if pending.state not in {DeliveryState.PENDING, DeliveryState.AWAITING_APPROVAL}:
            raise ValueError(f"Delivery is {pending.state.value}")
        if pending.expires_at <= datetime.now(UTC):
            expired = store.update_delivery(
                delivery_id,
                DeliveryState.EXPIRED,
                error="Delivery review expired.",
            )
            self._audit_delivery("command_center_delivery_expired", expired, "expired")
            raise TimeoutError("Delivery review expired")
        artifact = store.get(pending.artifact_id)
        if (
            artifact.revision != pending.artifact_revision
            or artifact.digest != pending.artifact_digest
        ):
            store.update_delivery(
                delivery_id,
                DeliveryState.INVALIDATED,
                error="Artifact revision changed after review.",
            )
            raise ValueError("Artifact revision changed after delivery review")
        destination = self.bindings.get_destination(pending.destination_binding_id)
        if destination_digest(destination) != pending.destination_digest:
            store.update_delivery(
                delivery_id,
                DeliveryState.INVALIDATED,
                error="Destination binding changed after review.",
            )
            raise ValueError("Destination changed after delivery review")
        snapshot = store.delivery_snapshot(delivery_id)
        if destination_digest(snapshot) != pending.destination_digest:
            raise ValueError("Stored delivery snapshot is invalid")
        definition = self.registry.destination(destination.destination_id).definition
        self._validate_destination_binding(destination, definition)
        if self.destination_status(destination).availability != BindingAvailability.AVAILABLE:
            raise PermissionError("Destination is no longer available")
        if artifact.sensitive and not destination.allow_sensitive:
            raise PermissionError("Destination no longer accepts sensitive artifacts")
        return pending, artifact, destination, definition

    def _record_delivery_outcome(
        self,
        pending: PendingDelivery,
        outcome: ExecutionOutcome,
    ) -> PendingDelivery:
        if outcome.permission_requests or outcome.registration_requests or outcome.confirmation:
            updated = self._artifact_store().update_delivery(
                pending.delivery_id,
                DeliveryState.AWAITING_APPROVAL,
                correlation_id=(
                    outcome.call.correlation_id if outcome.call else pending.correlation_id
                ),
            )
            self._audit_delivery(
                "command_center_delivery_awaiting_approval",
                updated,
                "awaiting_approval",
            )
            return updated
        if outcome.result is not None:
            return self._finish_delivery(pending, outcome.result)
        raise RuntimeError("Delivery preparation returned no result or review request")

    def _finish_delivery(
        self,
        pending: PendingDelivery,
        result: SkillResult,
    ) -> PendingDelivery:
        state = (
            DeliveryState.SUCCEEDED
            if result.status == ResultStatus.COMPLETED
            else DeliveryState.CANCELLED
            if result.status == ResultStatus.CANCELLED
            else DeliveryState.FAILED
        )
        error = result.error.message if result.error else None
        updated = self._artifact_store().update_delivery(
            pending.delivery_id,
            state,
            correlation_id=pending.correlation_id,
            result_status=result.status,
            error=error,
        )
        self._audit_delivery("command_center_delivery_finished", updated, state.value)
        return updated

    @staticmethod
    def _destination_target_preview(
        binding: DestinationBinding,
        definition: DestinationDefinition,
    ) -> str:
        values = binding.configuration
        if definition.local_sink == "assistant":
            return "Local Assistant response"
        if definition.local_sink == "inbox":
            return "Local Assistant Inbox"
        if definition.local_sink == "notification":
            return f"Desktop notification: {values.get('title', 'Local Assistant')}"
        if definition.local_sink == "export":
            return f"New local file: {values.get('path')}"
        if "recipients" in values:
            return "Draft email to " + ", ".join(str(value) for value in values["recipients"])
        if "channel_id" in values:
            return f"Slack channel {values['channel_id']}"
        if "resource_id" in values:
            return f"Notion resource {values['resource_id']}"
        if "owner" in values and "repo" in values:
            target = f"GitHub {values['owner']}/{values['repo']}"
            if "issue_number" in values:
                target += f" issue #{values['issue_number']}"
            return target
        return definition.display_name

    def _artifact_store(self) -> CommandCenterArtifactStore:
        if self.artifacts is None:
            raise RuntimeError("Command-center artifact storage is not configured")
        return self.artifacts

    def _audit_artifact(
        self,
        event_type: str,
        artifact: WorkArtifact,
        decision: str,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            correlation_id=str(artifact.artifact_id),
            event_type=event_type,
            decision=decision,
            details={
                "artifact_id": str(artifact.artifact_id),
                "revision": artifact.revision,
                "digest": artifact.digest,
                "sensitive": artifact.sensitive,
                "content_omitted": True,
            },
        )

    def _audit_delivery(
        self,
        event_type: str,
        delivery: PendingDelivery,
        decision: str,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            correlation_id=str(delivery.correlation_id or delivery.delivery_id),
            event_type=event_type,
            decision=decision,
            details={
                "delivery_id": str(delivery.delivery_id),
                "artifact_id": str(delivery.artifact_id),
                "artifact_revision": delivery.artifact_revision,
                "destination_binding_id": str(delivery.destination_binding_id),
                "target_preview": delivery.target_preview,
                "content_omitted": True,
            },
        )

    def _normalize_source_result(
        self,
        binding: SourceBinding,
        definition: SourceDefinition,
        result: SkillResult,
        *,
        duration_ms: float,
    ) -> SourceResult:
        candidates = result.items
        if candidates is None:
            candidates = self._structured_result_items(result)
        normalized: list[SourceResultItem] = []
        characters = 0
        truncated = False
        for index, item in enumerate(candidates or (), 1):
            if len(normalized) >= binding.max_items:
                truncated = True
                break
            title = self._clean_text(item.title, limit=500) or f"Item {index}"
            description = self._clean_text(item.description, limit=2_000)
            item_characters = len(title) + len(description or "")
            if characters + item_characters > binding.max_characters:
                truncated = True
                break
            normalized.append(
                SourceResultItem(
                    item_id=self._clean_text(item.item_id, limit=100) or f"item-{index}",
                    title=title,
                    description=description,
                    kind=self._clean_text(item.kind, limit=80) or "generic",
                    sensitive=item.sensitive,
                )
            )
            characters += item_characters
        error = self._clean_text(result.error.message, limit=1_000) if result.error else None
        return SourceResult(
            binding_id=binding.binding_id,
            source_id=binding.source_id,
            provider_id=definition.provider_id,
            display_name=binding.display_name,
            status=result.status,
            collected_at=datetime.now(UTC),
            duration_ms=duration_ms,
            summary=self._clean_text(result.summary, limit=1_000) or _status_text(result.status),
            items=tuple(normalized),
            error=error,
            sensitive=definition.sensitive
            or result.sensitive
            or any(item.sensitive for item in normalized),
            truncated=truncated,
        )

    @staticmethod
    def _structured_result_items(result: SkillResult):
        from assistant.contracts import RequestResultItem

        for key in ("items", "resources", "results", "events", "messages", "files", "issues"):
            values = result.data.get(key)
            if not isinstance(values, list):
                continue
            items = []
            for index, value in enumerate(values, 1):
                if not isinstance(value, dict):
                    continue
                title = next(
                    (
                        value.get(field)
                        for field in ("title", "name", "subject", "summary", "path", "id")
                        if isinstance(value.get(field), str) and value.get(field).strip()
                    ),
                    f"Item {index}",
                )
                description = next(
                    (
                        value.get(field)
                        for field in ("description", "status", "state", "type")
                        if isinstance(value.get(field), str) and value.get(field).strip()
                    ),
                    None,
                )
                items.append(
                    RequestResultItem(
                        item_id=f"structured-{index}",
                        title=str(title),
                        value={},
                        description=str(description) if description else None,
                    )
                )
            return tuple(items)
        return ()

    @staticmethod
    def _source_failure(
        binding: SourceBinding,
        definition: SourceDefinition,
        status: ResultStatus,
        error: str,
        *,
        duration_ms: float = 0,
        required_capabilities: tuple[str, ...] = (),
    ) -> SourceResult:
        cleaned = CommandCenterService._clean_text(error, limit=1_000) or "Source failed."
        return SourceResult(
            binding_id=binding.binding_id,
            source_id=binding.source_id,
            provider_id=definition.provider_id,
            display_name=binding.display_name,
            status=status,
            duration_ms=duration_ms,
            summary=cleaned,
            error=cleaned,
            required_capabilities=required_capabilities,
            sensitive=definition.sensitive,
        )

    @staticmethod
    def _mutating_capability(capability: str) -> bool:
        return capability in {
            "device.control",
            "filesystem.write",
            "input.control",
            "integration.write",
            "media.control",
            "messaging",
            "notification.create",
            "process.launch",
            "process.terminate",
            "process.window",
            "settings.set",
            "system.display",
            "system.network",
            "system.power",
        } or capability.endswith(".write")

    @staticmethod
    def _mutating_operation(operation: str) -> bool:
        action = operation.strip().casefold().replace("-", "_")
        verb = action.split("_", 1)[0]
        return verb in {
            "add",
            "append",
            "archive",
            "cancel",
            "complete",
            "copy",
            "create",
            "delete",
            "disconnect",
            "dismiss",
            "grant",
            "label",
            "mark",
            "modify",
            "move",
            "post",
            "publish",
            "remove",
            "rename",
            "reply",
            "respond",
            "revoke",
            "send",
            "set",
            "share",
            "start",
            "stop",
            "terminate",
            "trash",
            "unarchive",
            "update",
            "write",
        }

    @staticmethod
    def _clean_text(value: object, *, limit: int) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).replace("\x00", "").split())
        return text[:limit].strip() or None

    @classmethod
    def _markdown_text(cls, value: object) -> str:
        text = cls._clean_text(value, limit=2_000) or ""
        for character in ("\\", "*", "_", "[", "]", "<", ">", "#", "`"):
            text = text.replace(character, f"\\{character}")
        return text


def _status_text(status: ResultStatus) -> str:
    return status.value.replace("_", " ").replace("partially completed", "partially completed")
