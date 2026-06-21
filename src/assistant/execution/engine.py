from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from assistant.contracts import (
    ConfirmationRequest,
    ExecutionPlan,
    PermissionScope,
    PipelineEvent,
    PipelineState,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillError,
    SkillResult,
)
from assistant.events import EventBus
from assistant.policy.confirmations import ConfirmationManager
from assistant.policy.permissions import PermissionManager
from assistant.skills.base import ApplicationRegistrationRequired
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import AuditStore, MetricsStore

logger = logging.getLogger("assistant.execution")


@dataclass(frozen=True)
class PermissionGrantRequest:
    call: SkillCall
    scope: PermissionScope


@dataclass(frozen=True)
class ApplicationRegistrationRequest:
    call: SkillCall
    application: str


@dataclass(frozen=True)
class ExecutionOutcome:
    result: SkillResult | None = None
    confirmation: ConfirmationRequest | None = None
    call: SkillCall | None = None
    permission_requests: tuple[PermissionGrantRequest, ...] = ()
    registration_requests: tuple[ApplicationRegistrationRequest, ...] = ()


@dataclass(frozen=True)
class ActiveJob:
    correlation_id: UUID
    skill_id: str
    display_name: str
    arguments: dict[str, Any]
    started_at: datetime
    elapsed_seconds: float


class ExecutionEngine:
    def __init__(
        self,
        registry: SkillRegistry,
        permissions: PermissionManager,
        confirmations: ConfirmationManager,
        audit: AuditStore,
        events: EventBus,
        metrics: MetricsStore | None = None,
        completed_key_limit: int = 1024,
        confirmation_overrides: Mapping[str, bool] | None = None,
    ) -> None:
        self.registry = registry
        self.permissions = permissions
        self.confirmations = confirmations
        self.audit = audit
        self.events = events
        self.metrics = metrics
        self.completed_key_limit = completed_key_limit
        self.confirmation_overrides = dict(confirmation_overrides or {})
        self._jobs: dict[UUID, asyncio.Task[SkillResult]] = {}
        self._job_details: dict[UUID, tuple[str, str, dict[str, Any], datetime, float]] = {}
        self._job_loops: dict[UUID, asyncio.AbstractEventLoop] = {}
        self._cancel_events: dict[UUID, asyncio.Event] = {}
        self._completed_keys: OrderedDict[str, SkillResult] = OrderedDict()

    def active_jobs(self) -> tuple[ActiveJob, ...]:
        current = time.monotonic()
        return tuple(
            ActiveJob(
                correlation_id=correlation_id,
                skill_id=skill_id,
                display_name=display_name,
                arguments=dict(arguments),
                started_at=started_at,
                elapsed_seconds=max(0.0, current - started_monotonic),
            )
            for correlation_id, (
                skill_id,
                display_name,
                arguments,
                started_at,
                started_monotonic,
            ) in tuple(self._job_details.items())
        )

    async def prepare(self, call: SkillCall) -> ExecutionOutcome:
        try:
            skill = self.registry.get(call.skill_id)
            skill.validate(call.arguments)
        except (KeyError, ValidationError, ValueError) as exc:
            return ExecutionOutcome(
                result=SkillResult(
                    status=ResultStatus.DENIED,
                    summary="The requested action was rejected.",
                    error=SkillError(code="invalid_call", message=str(exc)),
                )
            )
        try:
            required_permissions = skill.required_permissions(call)
            checks = tuple(skill.permission_checks(call))
        except ApplicationRegistrationRequired as exc:
            await self.events.publish(
                PipelineEvent(
                    correlation_id=call.correlation_id,
                    state=PipelineState.AWAITING_APPROVAL,
                    message="Application registration is required before this action can run.",
                    data={
                        "registrations": [
                            {
                                "skill_id": call.skill_id,
                                "application": exc.application,
                            }
                        ]
                    },
                )
            )
            return ExecutionOutcome(
                result=SkillResult(
                    status=ResultStatus.DENIED,
                    summary="Application registration is required before this action can run.",
                    error=SkillError(
                        code="application_not_registered",
                        message=str(exc),
                        recoverable=True,
                    ),
                    data={
                        "missing_registrations": [
                            {
                                "skill_id": call.skill_id,
                                "application": exc.application,
                            }
                        ]
                    },
                ),
                call=call,
                registration_requests=(
                    ApplicationRegistrationRequest(
                        call=call, application=exc.application
                    ),
                ),
            )
        decisions = [
            self.permissions.authorize(check_call, scopes, consume_once=False)
            for check_call, scopes in checks
        ]
        missing = tuple(
            PermissionGrantRequest(call=check_call, scope=scope)
            for (check_call, _), decision in zip(checks, decisions, strict=True)
            for scope in decision.missing
        )
        if any(not decision.allowed for decision in decisions):
            await self.events.publish(
                PipelineEvent(
                    correlation_id=call.correlation_id,
                    state=PipelineState.AWAITING_APPROVAL,
                    message="Permission is required before this action can run.",
                    data={
                        "permissions": [
                            {
                                "skill_id": request.call.skill_id,
                                **request.scope.model_dump(),
                            }
                            for request in missing
                        ]
                    },
                )
            )
            return ExecutionOutcome(
                result=SkillResult(
                    status=ResultStatus.DENIED,
                    summary="Permission is required before this action can run.",
                    error=SkillError(
                        code="permission_denied",
                        message="Required permission has not been granted",
                        recoverable=True,
                    ),
                    data={
                        "missing_permissions": [
                            {
                                "skill_id": request.call.skill_id,
                                **request.scope.model_dump(),
                            }
                            for request in missing
                        ]
                    },
                ),
                call=call,
                permission_requests=missing,
            )
        risk = skill.risk_for(call)
        if self._requires_confirmation(call, risk):
            preview = await skill.preview(call)
            targets = tuple(
                target
                for permission in required_permissions
                for target in permission.targets
            )
            confirmation = self.confirmations.create(
                call,
                exact_action=preview,
                targets=targets,
                risk=risk,
            )
            await self.events.publish(
                PipelineEvent(
                    correlation_id=call.correlation_id,
                    state=PipelineState.AWAITING_APPROVAL,
                    message=preview,
                    data=confirmation.model_dump(mode="json"),
                )
            )
            return ExecutionOutcome(confirmation=confirmation, call=call)
        return ExecutionOutcome(result=await self.execute(call), call=call)

    async def execute(
        self, call: SkillCall, *, confirmation_id: UUID | None = None, typed_token: str | None = None
    ) -> SkillResult:
        if call.idempotency_key in self._completed_keys:
            self._completed_keys.move_to_end(call.idempotency_key)
            return self._completed_keys[call.idempotency_key]
        try:
            skill = self.registry.get(call.skill_id)
            skill.validate(call.arguments)
        except (KeyError, ValidationError, ValueError) as exc:
            return self._failure("invalid_call", str(exc), ResultStatus.DENIED)
        required_permissions = skill.required_permissions(call)
        decisions = [
            self.permissions.authorize(check_call, scopes)
            for check_call, scopes in skill.permission_checks(call)
        ]
        if any(not decision.allowed for decision in decisions):
            return self._failure(
                "permission_denied",
                "Required permission is missing or outside its approved scope.",
                ResultStatus.DENIED,
            )
        risk = skill.risk_for(call)
        if self._requires_confirmation(call, risk):
            if confirmation_id is None or not self.confirmations.approve(
                confirmation_id, call, typed_token=typed_token
            ):
                return self._failure(
                    "confirmation_required",
                    "A matching, unexpired confirmation is required.",
                    ResultStatus.DENIED,
                )
        cancel_event = asyncio.Event()
        self._cancel_events[call.correlation_id] = cancel_event
        await self.events.publish(
            PipelineEvent(
                correlation_id=call.correlation_id,
                state=PipelineState.EXECUTING,
                message=f"Executing {skill.manifest.display_name}",
            )
        )
        started = time.perf_counter()
        max_attempts = 2 if skill.manifest.safe_to_retry else 1
        timeout_seconds = (
            max(30.0, float(call.arguments.get("seconds", 0)) + 1.0)
            if call.skill_id == "core.timer"
            else 30.0
        )
        result = None
        attempts = 0
        try:
            while attempts < max_attempts:
                attempts += 1
                task = asyncio.create_task(skill.execute(call, cancel_event))
                self._jobs[call.correlation_id] = task
                self._job_loops[call.correlation_id] = asyncio.get_running_loop()
                self._job_details[call.correlation_id] = (
                    call.skill_id,
                    skill.manifest.display_name,
                    dict(call.arguments),
                    datetime.now(UTC),
                    time.monotonic(),
                )
                try:
                    result = await asyncio.wait_for(task, timeout=timeout_seconds)
                    skill.validate_result(result)
                    break
                except TimeoutError:
                    if attempts >= max_attempts:
                        cancel_event.set()
                        result = self._failure(
                            "skill_timeout",
                            "The skill exceeded its execution timeout.",
                            ResultStatus.TIMED_OUT,
                        )
                except asyncio.CancelledError:
                    result = self._failure(
                        "cancelled",
                        "The action was cancelled.",
                        ResultStatus.CANCELLED,
                    )
                    break
                except Exception as exc:
                    if attempts >= max_attempts:
                        result = self._failure(
                            "skill_failed", str(exc), ResultStatus.FAILED
                        )
        finally:
            self._jobs.pop(call.correlation_id, None)
            self._job_details.pop(call.correlation_id, None)
            self._job_loops.pop(call.correlation_id, None)
            self._cancel_events.pop(call.correlation_id, None)
        assert result is not None
        self.audit.record(
            correlation_id=str(call.correlation_id),
            event_type="skill_execution",
            skill_id=call.skill_id,
            decision=result.status.value,
            permissions=[item.capability for item in required_permissions],
            details={
                "arguments": call.arguments,
                "result": result.model_dump(mode="json"),
                "attempts": attempts,
            },
        )
        duration_ms = (time.perf_counter() - started) * 1000
        if self.metrics:
            self.metrics.record(
                correlation_id=str(call.correlation_id),
                stage="execution",
                duration_ms=duration_ms,
                outcome=result.status.value,
            )
        logger.info(
            "Skill execution finished",
            extra={
                "correlation_id": str(call.correlation_id),
                "stage": "execution",
                "skill_id": call.skill_id,
                "outcome": result.status.value,
            },
        )
        if result.status == ResultStatus.COMPLETED:
            self._completed_keys[call.idempotency_key] = result
            self._completed_keys.move_to_end(call.idempotency_key)
            while len(self._completed_keys) > self.completed_key_limit:
                self._completed_keys.popitem(last=False)
        return result

    def _requires_confirmation(self, call: SkillCall, risk: RiskLevel) -> bool:
        for key in self._confirmation_keys(call, risk):
            if key in self.confirmation_overrides:
                return self.confirmation_overrides[key]
        return risk != RiskLevel.LOW

    @staticmethod
    def _confirmation_keys(call: SkillCall, risk: RiskLevel) -> tuple[str, ...]:
        action_value = None
        for field in ("operation", "action", "setting"):
            value = call.arguments.get(field)
            if isinstance(value, str) and value.strip():
                action_value = value.strip().casefold()
                break
        keys = []
        if action_value:
            keys.append(f"skill:{call.skill_id}:{action_value}")
        keys.append(f"skill:{call.skill_id}")
        keys.append(f"risk:{risk.value}")
        return tuple(keys)

    async def execute_plan(self, plan: ExecutionPlan) -> dict[str, SkillResult]:
        results: dict[str, SkillResult] = {}
        pending = {step.step_id: step for step in plan.steps}

        async def run_step(step_id: str) -> tuple[str, SkillResult]:
            step = pending[step_id]
            try:
                return step_id, await asyncio.wait_for(
                    self.execute(step.call), timeout=step.timeout_seconds
                )
            except TimeoutError:
                return step_id, self._failure(
                    "step_timeout", f"Step {step_id} timed out.", ResultStatus.TIMED_OUT
                )

        try:
            async with asyncio.timeout(plan.timeout_seconds):
                while pending:
                    blocked = [
                        step_id
                        for step_id, step in pending.items()
                        if any(
                            dependency in results
                            and results[dependency].status != ResultStatus.COMPLETED
                            for dependency in step.depends_on
                        )
                    ]
                    for step_id in blocked:
                        results[step_id] = self._failure(
                            "failed_prerequisite",
                            f"Step {step_id} did not run because a prerequisite failed.",
                            ResultStatus.CANCELLED,
                        )
                        pending.pop(step_id)
                    ready = [
                        step_id
                        for step_id, step in pending.items()
                        if all(dependency in results for dependency in step.depends_on)
                    ]
                    if not ready and pending:
                        raise ValueError("Plan contains a dependency cycle or missing dependency")
                    completed = await asyncio.gather(*(run_step(step_id) for step_id in ready))
                    for step_id, result in completed:
                        results[step_id] = result
                        pending.pop(step_id)
        except TimeoutError:
            await self.cancel()
            for step_id in pending:
                results[step_id] = self._failure(
                    "plan_timeout",
                    f"Step {step_id} did not finish before the plan timeout.",
                    ResultStatus.TIMED_OUT,
                )
        return results

    async def cancel(self, correlation_id: UUID | None = None) -> None:
        self.confirmations.cancel_all()
        targets = (
            [correlation_id]
            if correlation_id is not None
            else list(self._cancel_events.keys())
        )
        for target in targets:
            event = self._cancel_events.get(target)
            task = self._jobs.get(target)
            job_loop = self._job_loops.get(target)
            current_loop = asyncio.get_running_loop()

            def request_cancel(
                cancel_event: asyncio.Event | None = event,
                active_task: asyncio.Task[SkillResult] | None = task,
            ) -> None:
                if cancel_event:
                    cancel_event.set()
                if active_task:
                    active_task.cancel()

            if job_loop and job_loop is not current_loop and job_loop.is_running():
                job_loop.call_soon_threadsafe(request_cancel)
            else:
                request_cancel()

    @staticmethod
    def _failure(code: str, message: str, status: ResultStatus) -> SkillResult:
        return SkillResult(
            status=status,
            summary={
                ResultStatus.DENIED: "The action was not allowed.",
                ResultStatus.CANCELLED: "The action was cancelled.",
                ResultStatus.TIMED_OUT: "The action timed out.",
            }.get(status, "The action failed."),
            error=SkillError(code=code, message=message, recoverable=status != ResultStatus.FAILED),
        )
