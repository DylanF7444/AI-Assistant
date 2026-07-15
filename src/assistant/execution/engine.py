from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

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
from assistant.execution.evaluation import (
    ApplicationRegistrationRequest,
    CallEvaluation,
    CallEvaluator,
    PermissionGrantRequest,
)
from assistant.policy.confirmations import ConfirmationManager
from assistant.policy.permissions import PermissionManager
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import AuditStore, MetricsStore

logger = logging.getLogger("assistant.execution")


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


@dataclass(frozen=True)
class UndoRecord:
    skill_id: str
    display_name: str
    rollback_token: str | None
    result_summary: str


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
        self.evaluator = CallEvaluator(
            registry,
            permissions,
            self._requires_confirmation,
        )
        self._jobs: dict[UUID, asyncio.Task[SkillResult]] = {}
        self._job_details: dict[UUID, tuple[str, str, dict[str, Any], datetime, float]] = {}
        self._job_loops: dict[UUID, asyncio.AbstractEventLoop] = {}
        self._cancel_events: dict[UUID, asyncio.Event] = {}
        self._completed_keys: OrderedDict[str, SkillResult] = OrderedDict()
        self._last_command: UndoRecord | None = None
        self._state_lock = threading.RLock()

    def active_jobs(self) -> tuple[ActiveJob, ...]:
        current = time.monotonic()
        with self._state_lock:
            details = tuple(self._job_details.items())
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
            ) in details
        )

    async def prepare(self, call: SkillCall) -> ExecutionOutcome:
        evaluation = await self.evaluator.evaluate(
            call,
            consume_once=False,
            include_preview=True,
        )
        if evaluation.invalid_error:
            return self._invalid_outcome(evaluation)
        if evaluation.registration_requests:
            return await self._registration_outcome(
                call,
                evaluation.registration_requests,
            )
        if evaluation.policy_denied:
            return await self._policy_outcome(call, evaluation.policy_denied)
        if evaluation.permission_requests:
            return await self._permission_outcome(call, evaluation.permission_requests)
        if evaluation.needs_confirmation:
            return await self._confirmation_outcome(evaluation)
        return ExecutionOutcome(result=await self.execute(call), call=call)

    async def request_confirmation(
        self,
        call: SkillCall,
        *,
        ttl_seconds: int = 30,
    ) -> ExecutionOutcome:
        """Validate a call and create a confirmation without executing it.

        Background approval uses this path so global interactive confirmation
        overrides can never turn a reviewed occurrence into immediate execution.
        """
        evaluation = await self.evaluator.evaluate(
            call,
            consume_once=False,
            include_preview=True,
            force_confirmation=True,
        )
        if evaluation.invalid_error:
            return self._invalid_outcome(evaluation)
        if evaluation.registration_requests:
            return await self._registration_outcome(
                call,
                evaluation.registration_requests,
            )
        if evaluation.policy_denied:
            return await self._policy_outcome(call, evaluation.policy_denied)
        if evaluation.permission_requests or not evaluation.allowed:
            return await self._permission_outcome(
                call,
                evaluation.permission_requests,
            )
        return await self._confirmation_outcome(
            evaluation,
            ttl_seconds=ttl_seconds,
        )

    async def prepare_plan(self, plan: ExecutionPlan) -> ExecutionOutcome:
        permission_requests: list[PermissionGrantRequest] = []
        registration_requests: list[ApplicationRegistrationRequest] = []
        confirmations: list[ConfirmationRequest] = []

        for step in plan.steps:
            evaluation = await self.evaluator.evaluate(
                step.call,
                consume_once=False,
                include_preview=True,
            )
            if evaluation.invalid_error:
                return self._invalid_outcome(evaluation, plan=True)
            if evaluation.policy_denied:
                return await self._policy_outcome(
                    step.call,
                    evaluation.policy_denied,
                    plan=True,
                )
            registration_requests.extend(evaluation.registration_requests)
            permission_requests.extend(evaluation.permission_requests)
            if evaluation.needs_confirmation:
                confirmations.append(self._create_confirmation(evaluation))

        first_call = plan.steps[0].call
        if registration_requests:
            return await self._registration_outcome(
                first_call,
                tuple(registration_requests),
                plan=True,
                permission_requests=tuple(permission_requests),
            )
        if permission_requests:
            return await self._permission_outcome(
                first_call,
                tuple(permission_requests),
                plan=True,
            )
        if confirmations:
            return await self._publish_confirmation(first_call, confirmations[0])
        return ExecutionOutcome(
            result=self._aggregate_plan_results(await self.execute_plan(plan)),
            call=first_call,
        )

    @staticmethod
    def _invalid_outcome(
        evaluation: CallEvaluation,
        *,
        plan: bool = False,
    ) -> ExecutionOutcome:
        assert evaluation.invalid_error is not None
        subject = "plan" if plan else "action"
        return ExecutionOutcome(
            result=SkillResult(
                status=ResultStatus.DENIED,
                summary=f"The requested {subject} was rejected.",
                error=evaluation.invalid_error,
            ),
            call=evaluation.call,
        )

    async def _registration_outcome(
        self,
        call: SkillCall,
        requests: tuple[ApplicationRegistrationRequest, ...],
        *,
        plan: bool = False,
        permission_requests: tuple[PermissionGrantRequest, ...] = (),
    ) -> ExecutionOutcome:
        subject = "plan" if plan else "action"
        message = f"Application registration is required before this {subject} can run."
        payload = [
            {
                "skill_id": request.call.skill_id,
                "application": request.application,
            }
            for request in requests
        ]
        await self.events.publish(
            PipelineEvent(
                correlation_id=call.correlation_id,
                state=PipelineState.AWAITING_APPROVAL,
                message=message,
                data={"registrations": payload},
            )
        )
        error_message = (
            f"Application is not registered: {requests[0].application}"
            if len(requests) == 1
            else "One or more applications must be registered."
        )
        return ExecutionOutcome(
            result=SkillResult(
                status=ResultStatus.DENIED,
                summary=message,
                error=SkillError(
                    code="application_not_registered",
                    message=error_message,
                    recoverable=True,
                ),
                data={"missing_registrations": payload},
            ),
            call=call,
            permission_requests=permission_requests,
            registration_requests=requests,
        )

    async def _policy_outcome(
        self,
        call: SkillCall,
        denied: tuple[PermissionScope, ...],
        *,
        plan: bool = False,
    ) -> ExecutionOutcome:
        payload = [scope.model_dump() for scope in denied]
        await self.events.publish(
            PipelineEvent(
                correlation_id=call.correlation_id,
                state=PipelineState.ERROR,
                message=denied[0].reason,
                data={"policy_denied": payload},
            )
        )
        subject = "plan" if plan else "action"
        return ExecutionOutcome(
            result=SkillResult(
                status=ResultStatus.DENIED,
                summary=f"The file access policy blocked this {subject}.",
                error=SkillError(
                    code="file_policy_denied",
                    message=denied[0].reason,
                    recoverable=False,
                ),
                data={"policy_denied": payload},
            ),
            call=call,
        )

    async def _permission_outcome(
        self,
        call: SkillCall,
        requests: tuple[PermissionGrantRequest, ...],
        *,
        plan: bool = False,
    ) -> ExecutionOutcome:
        subject = "plan" if plan else "action"
        message = f"Permission is required before this {subject} can run."
        payload = [
            {
                "skill_id": request.call.skill_id,
                **request.scope.model_dump(),
            }
            for request in requests
        ]
        await self.events.publish(
            PipelineEvent(
                correlation_id=call.correlation_id,
                state=PipelineState.AWAITING_APPROVAL,
                message=message,
                data={"permissions": payload},
            )
        )
        return ExecutionOutcome(
            result=SkillResult(
                status=ResultStatus.DENIED,
                summary=message,
                error=SkillError(
                    code="permission_denied",
                    message="Required permission has not been granted",
                    recoverable=True,
                ),
                data={"missing_permissions": payload},
            ),
            call=call,
            permission_requests=requests,
        )

    def _create_confirmation(
        self,
        evaluation: CallEvaluation,
        *,
        ttl_seconds: int = 30,
    ) -> ConfirmationRequest:
        assert evaluation.risk is not None
        assert evaluation.preview is not None
        targets = tuple(
            target
            for permission in evaluation.required_permissions
            for target in permission.targets
        )
        return self.confirmations.create(
            evaluation.call,
            exact_action=evaluation.preview,
            targets=targets,
            risk=evaluation.risk,
            ttl_seconds=ttl_seconds,
        )

    async def _confirmation_outcome(
        self,
        evaluation: CallEvaluation,
        *,
        ttl_seconds: int = 30,
    ) -> ExecutionOutcome:
        confirmation = self._create_confirmation(
            evaluation,
            ttl_seconds=ttl_seconds,
        )
        return await self._publish_confirmation(evaluation.call, confirmation)

    async def _publish_confirmation(
        self,
        call: SkillCall,
        confirmation: ConfirmationRequest,
    ) -> ExecutionOutcome:
        await self.events.publish(
            PipelineEvent(
                correlation_id=confirmation.correlation_id,
                state=PipelineState.AWAITING_APPROVAL,
                message=confirmation.exact_action,
                data=confirmation.model_dump(mode="json"),
            )
        )
        return ExecutionOutcome(confirmation=confirmation, call=call)

    async def execute(
        self,
        call: SkillCall,
        *,
        confirmation_id: UUID | None = None,
        typed_token: str | None = None,
    ) -> SkillResult:
        with self._state_lock:
            if call.idempotency_key in self._completed_keys:
                self._completed_keys.move_to_end(call.idempotency_key)
                return self._completed_keys[call.idempotency_key]
        evaluation = await self.evaluator.evaluate(
            call,
            consume_once=True,
            include_preview=False,
        )
        if evaluation.invalid_error:
            return self._failure(
                evaluation.invalid_error.code,
                evaluation.invalid_error.message,
                ResultStatus.DENIED,
            )
        if evaluation.registration_requests:
            return self._failure(
                "application_not_registered",
                f"Application is not registered: {evaluation.registration_requests[0].application}",
                ResultStatus.DENIED,
            )
        if evaluation.policy_denied:
            return self._failure(
                "file_policy_denied",
                evaluation.policy_denied[0].reason,
                ResultStatus.DENIED,
            )
        if evaluation.permission_requests or not evaluation.allowed:
            return self._failure(
                "permission_denied",
                "Required permission is missing or outside its approved scope.",
                ResultStatus.DENIED,
            )
        assert evaluation.skill is not None
        assert evaluation.risk is not None
        skill = evaluation.skill
        required_permissions = evaluation.required_permissions
        if self._requires_confirmation(call, evaluation.risk) and (
            confirmation_id is None
            or not self.confirmations.approve(confirmation_id, call, typed_token=typed_token)
        ):
            return self._failure(
                "confirmation_required",
                "A matching, unexpired confirmation is required.",
                ResultStatus.DENIED,
            )
        cancel_event = asyncio.Event()
        with self._state_lock:
            self._cancel_events[call.correlation_id] = cancel_event
        await self.events.publish(
            PipelineEvent(
                correlation_id=call.correlation_id,
                state=PipelineState.EXECUTING,
                message=f"Executing {skill.manifest.display_name}",
            )
        )
        started = time.perf_counter()
        max_attempts = (
            1
            if call.permission_context.invocation == "background"
            else 2
            if skill.manifest.safe_to_retry
            else 1
        )
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
                with self._state_lock:
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
                        result = self._failure("skill_failed", str(exc), ResultStatus.FAILED)
        finally:
            with self._state_lock:
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
            with self._state_lock:
                if call.permission_context.invocation == "interactive":
                    self._last_command = UndoRecord(
                        skill_id=call.skill_id,
                        display_name=skill.manifest.display_name,
                        rollback_token=(
                            result.rollback_token if skill.manifest.supports_rollback else None
                        ),
                        result_summary=result.summary,
                    )
                self._completed_keys[call.idempotency_key] = result
                self._completed_keys.move_to_end(call.idempotency_key)
                while len(self._completed_keys) > self.completed_key_limit:
                    self._completed_keys.popitem(last=False)
        return result

    async def undo_last(self, correlation_id: UUID | None = None) -> SkillResult:
        undo_correlation_id = correlation_id or uuid4()
        with self._state_lock:
            record = self._last_command
        if record is None:
            return self._failure(
                "undo_unavailable",
                "There is no completed command available to undo.",
                ResultStatus.DENIED,
            ).model_copy(update={"summary": "There is no command available to undo."})
        if not record.rollback_token:
            return self._failure(
                "undo_not_supported",
                f"{record.display_name} cannot be undone.",
                ResultStatus.DENIED,
            ).model_copy(
                update={
                    "summary": "The last command cannot be undone.",
                    "data": {
                        "skill_id": record.skill_id,
                        "display_name": record.display_name,
                        "result_summary": record.result_summary,
                    },
                }
            )
        try:
            skill = self.registry.get(record.skill_id)
        except KeyError as exc:
            return self._failure(
                "undo_unavailable",
                f"The skill that performed the last command is no longer available: {exc}",
                ResultStatus.DENIED,
            )
        await self.events.publish(
            PipelineEvent(
                correlation_id=undo_correlation_id,
                state=PipelineState.EXECUTING,
                message=f"Undoing {record.display_name}",
            )
        )
        try:
            result = await skill.rollback(record.rollback_token)
        except NotImplementedError:
            return self._failure(
                "undo_not_supported",
                f"{record.display_name} cannot be undone.",
                ResultStatus.DENIED,
            )
        except Exception as exc:
            return self._failure("undo_failed", str(exc), ResultStatus.FAILED)
        self.audit.record(
            correlation_id=str(undo_correlation_id),
            event_type="skill_rollback",
            skill_id=record.skill_id,
            decision=result.status.value,
            permissions=[],
            details={
                "result": result.model_dump(mode="json"),
                "undid": {
                    "skill_id": record.skill_id,
                    "display_name": record.display_name,
                    "result_summary": record.result_summary,
                },
            },
        )
        if result.status == ResultStatus.COMPLETED:
            with self._state_lock:
                if self._last_command == record:
                    self._last_command = None
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

    async def execute_plan(
        self,
        plan: ExecutionPlan,
        *,
        confirmations: Mapping[str, tuple[UUID, str | None]] | None = None,
    ) -> dict[str, SkillResult]:
        results: dict[str, SkillResult] = {}
        pending = {step.step_id: step for step in plan.steps}
        approvals = dict(confirmations or {})

        async def run_step(step_id: str) -> tuple[str, SkillResult]:
            step = pending[step_id]
            confirmation_id, typed_token = approvals.get(step_id, (None, None))
            try:
                return step_id, await asyncio.wait_for(
                    self.execute(
                        step.call,
                        confirmation_id=confirmation_id,
                        typed_token=typed_token,
                    ),
                    timeout=step.timeout_seconds,
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

    @staticmethod
    def _aggregate_plan_results(results: Mapping[str, SkillResult]) -> SkillResult:
        if all(result.status == ResultStatus.COMPLETED for result in results.values()):
            status = ResultStatus.COMPLETED
            summary = "Plan completed."
        elif any(result.status == ResultStatus.COMPLETED for result in results.values()):
            status = ResultStatus.PARTIAL
            summary = "Plan partially completed."
        else:
            status = ResultStatus.FAILED
            summary = "Plan failed."
        display_lines = []
        for step_id, result in results.items():
            detail = result.display_text or result.summary
            if result.error:
                detail = f"{detail}\n{result.error.code}: {result.error.message}"
            display_lines.append(f"{step_id}: {detail}")
        return SkillResult(
            status=status,
            summary=summary,
            display_text="\n\n".join(display_lines),
            data={
                "steps": {
                    step_id: result.model_dump(mode="json") for step_id, result in results.items()
                }
            },
            sensitive=any(result.sensitive for result in results.values()),
        )

    async def cancel(self, correlation_id: UUID | None = None) -> None:
        self.confirmations.cancel_all()
        with self._state_lock:
            targets = (
                [correlation_id] if correlation_id is not None else list(self._cancel_events.keys())
            )
        for target in targets:
            with self._state_lock:
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
