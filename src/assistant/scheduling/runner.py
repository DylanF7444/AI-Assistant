from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import timedelta
from uuid import UUID, uuid4

from assistant.contracts import (
    ActivityTrace,
    ExecutionPlan,
    PipelineEvent,
    PipelineState,
    PlanStep,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
    TracePhase,
)
from assistant.execution.engine import ExecutionEngine
from assistant.policy.background import background_compatibility
from assistant.scheduling.clock import Clock
from assistant.scheduling.contracts import (
    ApprovalConfirmation,
    ApprovalReview,
    FixedSkillAction,
    FrozenPlanAction,
    NotificationAction,
    OneTimeTrigger,
    ScheduleDefinition,
    ScheduleRun,
    ScheduleRunState,
    ScheduleStatus,
)
from assistant.scheduling.notifications import NotificationCenter, NotificationMessage
from assistant.scheduling.store import ScheduleStore, digest
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import AuditStore, MetricsStore
from assistant.windows.session import is_session_unlocked


class BackgroundRunner:
    def __init__(
        self,
        store: ScheduleStore,
        notifications: NotificationCenter,
        audit: AuditStore,
        clock: Clock | None = None,
        *,
        metrics: MetricsStore | None = None,
        engine: ExecutionEngine | None = None,
        registry: SkillRegistry | None = None,
        background_actions_enabled: bool = False,
        pause_while_locked: bool = True,
        session_unlocked: Callable[[], bool] = is_session_unlocked,
        notify_on_success: bool = True,
        notify_on_failure: bool = True,
        lease_seconds: int = 30,
    ) -> None:
        self.store = store
        self.notifications = notifications
        self.audit = audit
        self.clock = clock or store.clock
        self.metrics = metrics
        self.engine = engine
        self.registry = registry
        self.background_actions_enabled = background_actions_enabled
        self.pause_while_locked = pause_while_locked
        self.session_unlocked = session_unlocked
        self.notify_on_success = notify_on_success
        self.notify_on_failure = notify_on_failure
        self.lease_seconds = lease_seconds

    async def run(self, run: ScheduleRun) -> ScheduleRun:
        correlation_id = uuid4()
        evaluating = self.store.transition_run(
            run.run_id,
            expected=(ScheduleRunState.LEASED,),
            state=ScheduleRunState.EVALUATING,
            correlation_id=correlation_id,
        )
        heartbeat = (
            asyncio.create_task(self._lease_heartbeat(evaluating))
            if evaluating.lease_owner is not None
            else None
        )
        try:
            schedule = self.store.get(evaluating.schedule_id)
            await self._publish_trace(
                correlation_id,
                TracePhase.DECISION,
                "Background check",
                f"Evaluating {schedule.name} against its frozen authorization and safety limits.",
            )
            reason = self._denial_reason(schedule, evaluating)
            if reason:
                await self._publish_trace(
                    correlation_id,
                    TracePhase.RESULT,
                    "Background blocked",
                    f"{schedule.name}: {reason}",
                )
                return self._blocked(evaluating, correlation_id, reason)
            effective_risk = self._effective_risk(schedule)
            if effective_risk != RiskLevel.LOW:
                approval_expires = self.clock.now() + timedelta(
                    seconds=schedule.run_policy.approval_window_seconds
                )
                result = self.store.transition_run(
                    evaluating.run_id,
                    expected=(ScheduleRunState.EVALUATING,),
                    state=ScheduleRunState.AWAITING_APPROVAL,
                    correlation_id=correlation_id,
                    approval_expires_at=approval_expires,
                    blocked_reason=f"{effective_risk.value.title()} risk requires approval",
                )
                self.notifications.publish(
                    NotificationMessage(
                        title="Scheduled action needs approval",
                        message=f"{schedule.name} is waiting for your approval.",
                        severity="warning",
                        schedule_id=schedule.schedule_id,
                        run_id=result.run_id,
                    )
                )
                self._audit(result, "schedule_approval_requested", "awaiting_approval")
                await self._publish_trace(
                    correlation_id,
                    TracePhase.PERMISSION,
                    "Background approval",
                    f"{schedule.name} is waiting for explicit approval.",
                )
                return result
            running = self.store.transition_run(
                evaluating.run_id,
                expected=(ScheduleRunState.EVALUATING,),
                state=ScheduleRunState.RUNNING,
                correlation_id=correlation_id,
            )
            if isinstance(schedule.action, NotificationAction):
                await self._publish_trace(
                    correlation_id,
                    TracePhase.ACTION,
                    "Background action",
                    f"Delivering notification for {schedule.name}.",
                )
                result = self._deliver_notification(schedule, running, correlation_id)
                await self._publish_trace(
                    correlation_id,
                    TracePhase.RESULT,
                    "Background result",
                    f"{schedule.name} completed.",
                )
                return result
            if isinstance(schedule.action, FixedSkillAction):
                return await self._execute_fixed(schedule, running, correlation_id)
            if isinstance(schedule.action, FrozenPlanAction):
                return await self._execute_plan(schedule, running, correlation_id)
            return self._blocked(running, correlation_id, "Unsupported scheduled action type")
        except TimeoutError:
            current = self.store.get_run(run.run_id)
            if current.state == ScheduleRunState.RUNNING:
                timed_out = self.store.transition_run(
                    run.run_id,
                    expected=(ScheduleRunState.RUNNING,),
                    state=ScheduleRunState.TIMED_OUT,
                    correlation_id=correlation_id,
                    result_status=ResultStatus.TIMED_OUT.value,
                    error={"code": "background_timeout", "message": "Runtime budget expired"},
                )
                self._audit(timed_out, "schedule_run_failed", "timed_out")
                return timed_out
            raise
        except Exception as exc:
            current = self.store.get_run(run.run_id)
            if current.state in {
                ScheduleRunState.EVALUATING,
                ScheduleRunState.RUNNING,
            }:
                failed = self.store.transition_run(
                    run.run_id,
                    expected=(current.state,),
                    state=ScheduleRunState.FAILED,
                    correlation_id=correlation_id,
                    result_status=ResultStatus.FAILED.value,
                    error={"code": "scheduler_failure", "message": str(exc)},
                )
                self._audit(failed, "schedule_run_failed", "failed")
                return failed
            raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _publish_trace(
        self,
        correlation_id,
        phase: TracePhase,
        label: str,
        detail: str,
    ) -> None:
        if self.engine is None:
            return
        await self.engine.events.publish(
            PipelineEvent(
                correlation_id=correlation_id,
                state=PipelineState.EXECUTING,
                message=detail[:500],
                trace=ActivityTrace(
                    phase=phase,
                    label=label,
                    detail=detail[:500],
                    invocation="background",
                ),
                updates_foreground_status=False,
            )
        )

    async def _lease_heartbeat(self, run: ScheduleRun) -> None:
        assert run.lease_owner is not None
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = self.store.renew_lease(
                run.run_id,
                run.lease_owner,
                lease_seconds=self.lease_seconds,
            )
            if not renewed:
                return

    async def prepare_approval(self, run_id: UUID) -> ApprovalReview:
        run = self.store.get_run(run_id)
        if run.state != ScheduleRunState.AWAITING_APPROVAL:
            raise ValueError(f"Run is not awaiting approval: {run.state.value}")
        assert run.approval_expires_at is not None
        if run.approval_expires_at <= self.clock.now():
            expired = self.store.transition_run(
                run.run_id,
                expected=(ScheduleRunState.AWAITING_APPROVAL,),
                state=ScheduleRunState.EXPIRED,
                correlation_id=run.correlation_id,
                result_status=ResultStatus.CANCELLED.value,
                blocked_reason="Approval window expired",
            )
            self._audit(expired, "schedule_approval_expired", "expired")
            raise ValueError("Approval window expired")
        schedule = self.store.get(run.schedule_id)
        reason = self._denial_reason(schedule, run)
        if reason:
            self._blocked(run, run.correlation_id or uuid4(), reason)
            raise ValueError(reason)
        assert self.engine is not None
        confirmations = []
        for step_id, call in self._occurrence_calls(schedule, run):
            assert self.registry is not None
            risk = self.registry.get(call.skill_id).risk_for(call)
            if risk == RiskLevel.LOW:
                continue
            ttl_seconds = max(
                1,
                int((run.approval_expires_at - self.clock.now()).total_seconds()),
            )
            outcome = await self.engine.request_confirmation(
                call,
                ttl_seconds=ttl_seconds,
            )
            if outcome.confirmation is None:
                reason = (
                    outcome.result.error.message
                    if outcome.result and outcome.result.error
                    else "The scheduled action can no longer be approved"
                )
                self._blocked(run, run.correlation_id or uuid4(), reason)
                raise ValueError(reason)
            confirmations.append(
                ApprovalConfirmation(step_id=step_id, request=outcome.confirmation)
            )
        if not confirmations:
            raise ValueError("This occurrence does not require approval")
        return ApprovalReview(
            run_id=run.run_id,
            schedule_id=schedule.schedule_id,
            schedule_revision=schedule.revision,
            schedule_name=schedule.name,
            action_digest=run.action_digest,
            risk=self._effective_risk(schedule),
            confirmations=tuple(confirmations),
            approval_expires_at=run.approval_expires_at,
        )

    async def approve(
        self,
        run_id: UUID,
        confirmation_ids: Mapping[str, UUID],
        *,
        typed_tokens: Mapping[str, str] | None = None,
    ) -> ScheduleRun:
        run = self.store.get_run(run_id)
        if run.state != ScheduleRunState.AWAITING_APPROVAL:
            raise ValueError(f"Run is not awaiting approval: {run.state.value}")
        if run.approval_expires_at is None or run.approval_expires_at <= self.clock.now():
            expired = self.store.transition_run(
                run.run_id,
                expected=(ScheduleRunState.AWAITING_APPROVAL,),
                state=ScheduleRunState.EXPIRED,
                correlation_id=run.correlation_id,
                result_status=ResultStatus.CANCELLED.value,
                blocked_reason="Approval window expired",
            )
            self._audit(expired, "schedule_approval_expired", "expired")
            return expired
        schedule = self.store.get(run.schedule_id)
        reason = self._denial_reason(schedule, run)
        if reason:
            return self._blocked(run, run.correlation_id or uuid4(), reason)
        correlation_id = run.correlation_id or uuid4()
        approval_owner = uuid4()
        running = self.store.transition_run(
            run.run_id,
            expected=(ScheduleRunState.AWAITING_APPROVAL,),
            state=ScheduleRunState.RUNNING,
            correlation_id=correlation_id,
            lease_owner=approval_owner,
            lease_seconds=self.lease_seconds,
        )
        tokens = dict(typed_tokens or {})
        heartbeat = asyncio.create_task(self._lease_heartbeat(running))
        try:
            if isinstance(schedule.action, FixedSkillAction):
                return await self._execute_fixed(
                    schedule,
                    running,
                    correlation_id,
                    confirmation_id=confirmation_ids.get("action"),
                    typed_token=tokens.get("action"),
                )
            if isinstance(schedule.action, FrozenPlanAction):
                approvals = {
                    step_id: (confirmation_id, tokens.get(step_id))
                    for step_id, confirmation_id in confirmation_ids.items()
                }
                return await self._execute_plan(
                    schedule,
                    running,
                    correlation_id,
                    confirmations=approvals,
                )
            return self._blocked(running, correlation_id, "Unsupported approval action type")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    def reject(self, run_id: UUID, reason: str = "Rejected by user") -> ScheduleRun:
        run = self.store.transition_run(
            run_id,
            expected=(ScheduleRunState.AWAITING_APPROVAL,),
            state=ScheduleRunState.REJECTED,
            result_status=ResultStatus.CANCELLED.value,
            blocked_reason=reason,
        )
        self._audit(run, "schedule_approval_rejected", "rejected", reason=reason)
        return run

    def expire_pending_approvals(self) -> tuple[ScheduleRun, ...]:
        expired = self.store.expire_approvals(self.clock.now())
        for run in expired:
            self._audit(run, "schedule_approval_expired", "expired")
        return expired

    def _deliver_notification(
        self,
        schedule: ScheduleDefinition,
        run: ScheduleRun,
        correlation_id,
    ) -> ScheduleRun:
        assert isinstance(schedule.action, NotificationAction)
        self.notifications.publish(
            NotificationMessage(
                title=schedule.action.title,
                message=schedule.action.message,
                severity="info",
                schedule_id=schedule.schedule_id,
                run_id=run.run_id,
            )
        )
        result = self.store.transition_run(
            run.run_id,
            expected=(ScheduleRunState.RUNNING,),
            state=ScheduleRunState.SUCCEEDED,
            correlation_id=correlation_id,
            result_status=ResultStatus.COMPLETED.value,
            result={"delivered": True},
        )
        self._audit(result, "schedule_run_completed", "completed")
        return result

    async def _execute_fixed(
        self,
        schedule: ScheduleDefinition,
        run: ScheduleRun,
        correlation_id,
        *,
        confirmation_id: UUID | None = None,
        typed_token: str | None = None,
    ) -> ScheduleRun:
        assert isinstance(schedule.action, FixedSkillAction)
        assert self.engine is not None
        call = self._occurrence_calls(schedule, run)[0][1]
        result = await asyncio.wait_for(
            self.engine.execute(
                call,
                confirmation_id=confirmation_id,
                typed_token=typed_token,
            ),
            timeout=schedule.autonomy_policy.max_runtime_seconds,
        )
        return self._finish_skill_result(schedule, run, correlation_id, result)

    async def _execute_plan(
        self,
        schedule: ScheduleDefinition,
        run: ScheduleRun,
        correlation_id,
        *,
        confirmations: Mapping[str, tuple[UUID, str | None]] | None = None,
    ) -> ScheduleRun:
        assert isinstance(schedule.action, FrozenPlanAction)
        assert self.engine is not None
        calls = dict(self._occurrence_calls(schedule, run))
        steps = tuple(
            PlanStep(
                step_id=step.step_id,
                call=calls[step.step_id],
                depends_on=step.depends_on,
                timeout_seconds=min(
                    step.timeout_seconds,
                    schedule.autonomy_policy.max_runtime_seconds,
                ),
            )
            for step in schedule.action.plan.steps
        )
        results = await asyncio.wait_for(
            self.engine.execute_plan(
                ExecutionPlan(
                    plan_id=schedule.action.plan.plan_id,
                    steps=steps,
                    timeout_seconds=min(
                        schedule.action.plan.timeout_seconds,
                        schedule.autonomy_policy.max_runtime_seconds,
                    ),
                ),
                confirmations=confirmations,
            ),
            timeout=schedule.autonomy_policy.max_runtime_seconds,
        )
        statuses = {result.status for result in results.values()}
        if statuses == {ResultStatus.COMPLETED}:
            state = ScheduleRunState.SUCCEEDED
            result_status = ResultStatus.COMPLETED
        elif ResultStatus.TIMED_OUT in statuses:
            state = ScheduleRunState.TIMED_OUT
            result_status = ResultStatus.TIMED_OUT
        elif ResultStatus.CANCELLED in statuses:
            state = ScheduleRunState.CANCELLED
            result_status = ResultStatus.CANCELLED
        elif ResultStatus.DENIED in statuses:
            state = ScheduleRunState.BLOCKED
            result_status = ResultStatus.DENIED
        else:
            state = ScheduleRunState.FAILED
            result_status = ResultStatus.FAILED
        if (
            state == ScheduleRunState.FAILED
            and run.attempt < schedule.run_policy.max_attempts
            and self._all_safe_to_retry(schedule)
        ):
            return self._schedule_retry(
                schedule,
                run,
                correlation_id,
                result_status.value,
                {step_id: self._stored_skill_result(result) for step_id, result in results.items()},
            )
        completed = self.store.transition_run(
            run.run_id,
            expected=(ScheduleRunState.RUNNING,),
            state=state,
            correlation_id=correlation_id,
            result_status=result_status.value,
            result={
                step_id: self._stored_skill_result(result) for step_id, result in results.items()
            },
        )
        self._audit(
            completed,
            "schedule_run_completed"
            if state == ScheduleRunState.SUCCEEDED
            else "schedule_run_failed",
            result_status.value,
        )
        return completed

    @staticmethod
    def _occurrence_calls(
        schedule: ScheduleDefinition,
        run: ScheduleRun,
    ) -> tuple[tuple[str, SkillCall], ...]:
        correlation_id = run.correlation_id or uuid4()
        if isinstance(schedule.action, FixedSkillAction):
            return (
                (
                    "action",
                    schedule.action.call.model_copy(
                        update={
                            "correlation_id": correlation_id,
                            "idempotency_key": f"{run.idempotency_key}:action",
                        }
                    ),
                ),
            )
        assert isinstance(schedule.action, FrozenPlanAction)
        return tuple(
            (
                step.step_id,
                step.call.model_copy(
                    update={
                        "correlation_id": correlation_id,
                        "idempotency_key": f"{run.idempotency_key}:{step.step_id}",
                    }
                ),
            )
            for step in schedule.action.plan.steps
        )

    def _finish_skill_result(
        self,
        schedule: ScheduleDefinition,
        run: ScheduleRun,
        correlation_id,
        result: SkillResult,
    ) -> ScheduleRun:
        states = {
            ResultStatus.COMPLETED: ScheduleRunState.SUCCEEDED,
            ResultStatus.TIMED_OUT: ScheduleRunState.TIMED_OUT,
            ResultStatus.CANCELLED: ScheduleRunState.CANCELLED,
            ResultStatus.DENIED: ScheduleRunState.BLOCKED,
            ResultStatus.FAILED: ScheduleRunState.FAILED,
            ResultStatus.PARTIAL: ScheduleRunState.FAILED,
        }
        state = states[result.status]
        if (
            state == ScheduleRunState.FAILED
            and run.attempt < schedule.run_policy.max_attempts
            and self._all_safe_to_retry(schedule)
        ):
            return self._schedule_retry(
                schedule,
                run,
                correlation_id,
                result.status.value,
                self._stored_skill_result(result),
            )
        completed = self.store.transition_run(
            run.run_id,
            expected=(ScheduleRunState.RUNNING,),
            state=state,
            correlation_id=correlation_id,
            result_status=result.status.value,
            result=self._stored_skill_result(result),
            blocked_reason=result.summary if state == ScheduleRunState.BLOCKED else None,
        )
        self._audit(
            completed,
            "schedule_run_completed"
            if state == ScheduleRunState.SUCCEEDED
            else "schedule_run_failed",
            result.status.value,
        )
        return completed

    @staticmethod
    def _stored_skill_result(result: SkillResult) -> dict:
        value = result.model_dump(mode="json")
        if result.sensitive:
            value["data"] = {"redacted": True}
            value["display_text"] = None
            value["speech_text"] = None
        return AuditStore.redact(value)

    def _schedule_retry(
        self,
        schedule: ScheduleDefinition,
        run: ScheduleRun,
        correlation_id,
        result_status: str,
        result: dict,
    ) -> ScheduleRun:
        delay_seconds = min(300, 5 * (2 ** max(0, run.attempt - 1)))
        pending = self.store.transition_run(
            run.run_id,
            expected=(ScheduleRunState.RUNNING,),
            state=ScheduleRunState.PENDING,
            correlation_id=correlation_id,
            result_status=result_status,
            result=result,
            retry_at=self.clock.now() + timedelta(seconds=delay_seconds),
        )
        self._audit(
            pending,
            "schedule_run_retry_scheduled",
            "retry_scheduled",
            retry_at=pending.retry_at.isoformat() if pending.retry_at else None,
            max_attempts=schedule.run_policy.max_attempts,
        )
        return pending

    def _all_safe_to_retry(self, schedule: ScheduleDefinition) -> bool:
        assert self.registry is not None
        calls = (
            (schedule.action.call,)
            if isinstance(schedule.action, FixedSkillAction)
            else tuple(step.call for step in schedule.action.plan.steps)
        )
        return all(self.registry.get(call.skill_id).manifest.safe_to_retry for call in calls)

    def _denial_reason(self, schedule: ScheduleDefinition, run: ScheduleRun) -> str | None:
        if schedule.status not in {ScheduleStatus.ACTIVE, ScheduleStatus.COMPLETED}:
            return f"Schedule is {schedule.status.value}"
        if schedule.revision != run.schedule_revision:
            return "Schedule revision changed before execution"
        if digest(schedule.action) != run.action_digest:
            return "Scheduled action changed before execution"
        authorization = self.store.get_authorization(schedule.schedule_id)
        if authorization is None:
            return "Background authorization is missing or revoked"
        if authorization.schedule_revision != schedule.revision:
            return "Background authorization belongs to another revision"
        if authorization.machine_identity != self.store.machine_identity:
            self.store.revoke_authorization(
                schedule.schedule_id,
                "Background authorization belongs to another machine",
            )
            return "Background authorization belongs to another machine"
        if authorization.action_digest != run.action_digest:
            return "Background authorization does not match the action"
        if authorization.policy_digest != digest(schedule.autonomy_policy):
            return "Background authorization does not match the autonomy policy"
        if authorization.expires_at and authorization.expires_at <= self.clock.now():
            self.store.revoke_authorization(
                schedule.schedule_id,
                "Background authorization expired",
            )
            return "Background authorization expired"
        if isinstance(schedule.action, (FixedSkillAction, FrozenPlanAction)):
            if not self.background_actions_enabled:
                return "Background skill actions are disabled in settings"
            if self.engine is None or self.registry is None:
                return "Background execution services are unavailable"
            snapshot_reason = self._snapshot_denial_reason(schedule)
            if snapshot_reason:
                self.store.revoke_authorization(schedule.schedule_id, snapshot_reason)
                return snapshot_reason
            compatibility_reason = self._background_compatibility_reason(schedule)
            if compatibility_reason:
                return compatibility_reason
            if (
                self.pause_while_locked
                and schedule.autonomy_policy.requires_unlocked_session
                and not self.session_unlocked()
            ):
                return "The interactive Windows session is locked"
            now = self.clock.now()
            if (
                self.store.count_runs_since(
                    schedule.schedule_id,
                    now - timedelta(hours=1),
                    exclude_run_id=run.run_id,
                )
                >= schedule.autonomy_policy.max_runs_per_hour
            ):
                return "Hourly background run budget exhausted"
            if (
                self.store.count_runs_since(
                    schedule.schedule_id,
                    now - timedelta(days=1),
                    exclude_run_id=run.run_id,
                )
                >= schedule.autonomy_policy.max_runs_per_day
            ):
                return "Daily background run budget exhausted"
        return None

    def _background_compatibility_reason(self, schedule: ScheduleDefinition) -> str | None:
        assert self.registry is not None
        calls = (
            (schedule.action.call,)
            if isinstance(schedule.action, FixedSkillAction)
            else tuple(step.call for step in schedule.action.plan.steps)
        )
        for call in calls:
            try:
                skill = self.registry.get(call.skill_id)
            except KeyError:
                return f"Scheduled skill is unavailable: {call.skill_id}"
            permissions = tuple(skill.required_permissions(call))
            compatibility = background_compatibility(skill.manifest, permissions)
            if not compatibility.allowed:
                return (
                    f"Background action would interrupt the active user: {call.skill_id}. "
                    f"{compatibility.reason}"
                )
        return None

    def _snapshot_denial_reason(self, schedule: ScheduleDefinition) -> str | None:
        assert self.registry is not None
        calls = (
            (schedule.action.call,)
            if isinstance(schedule.action, FixedSkillAction)
            else tuple(step.call for step in schedule.action.plan.steps)
        )
        if len(calls) > schedule.autonomy_policy.max_steps:
            return "Plan exceeds the authorized step budget"
        if len(schedule.skill_snapshot) != len(calls):
            return "Scheduled action snapshot count changed"
        for call, snapshot in zip(calls, schedule.skill_snapshot, strict=True):
            if call.skill_id not in schedule.autonomy_policy.allowed_skill_ids:
                return f"Skill is outside the autonomy policy: {call.skill_id}"
            try:
                skill = self.registry.get(call.skill_id)
            except KeyError:
                return f"Scheduled skill is unavailable: {call.skill_id}"
            if snapshot.skill_id != call.skill_id:
                return f"Scheduled skill has no authorization snapshot: {call.skill_id}"
            if skill.manifest.version != snapshot.version:
                return f"Scheduled skill version changed: {call.skill_id}"
            permissions = tuple(skill.required_permissions(call))
            if (
                any(
                    permission.capability == "network"
                    or permission.capability.startswith("network.")
                    for permission in permissions
                )
                and not schedule.autonomy_policy.allow_network
            ):
                return f"Network access is outside the autonomy policy: {call.skill_id}"
            if (
                digest([permission.model_dump(mode="json") for permission in permissions])
                != snapshot.permissions_digest
            ):
                return f"Scheduled skill permissions changed: {call.skill_id}"
            if digest(skill.input_schema()) != snapshot.input_schema_digest:
                return f"Scheduled skill schema changed: {call.skill_id}"
            risk = skill.risk_for(call)
            if risk != snapshot.risk or self._risk_rank(risk) > self._risk_rank(
                schedule.autonomy_policy.max_risk
            ):
                return f"Scheduled skill risk changed: {call.skill_id}"
        return None

    def _effective_risk(self, schedule: ScheduleDefinition) -> RiskLevel:
        if isinstance(schedule.action, NotificationAction):
            return RiskLevel.LOW
        assert self.registry is not None
        calls = (
            (schedule.action.call,)
            if isinstance(schedule.action, FixedSkillAction)
            else tuple(step.call for step in schedule.action.plan.steps)
        )
        return max(
            (self.registry.get(call.skill_id).risk_for(call) for call in calls),
            key=self._risk_rank,
            default=RiskLevel.LOW,
        )

    @staticmethod
    def _risk_rank(risk: RiskLevel) -> int:
        return {
            RiskLevel.LOW: 0,
            RiskLevel.MEDIUM: 1,
            RiskLevel.HIGH: 2,
            RiskLevel.CRITICAL: 3,
        }[risk]

    def _blocked(self, run: ScheduleRun, correlation_id, reason: str) -> ScheduleRun:
        result = self.store.transition_run(
            run.run_id,
            expected=(run.state,),
            state=ScheduleRunState.BLOCKED,
            correlation_id=correlation_id,
            blocked_reason=reason,
        )
        self._audit(result, "schedule_run_blocked", "blocked", reason=reason)
        return result

    def _audit(
        self,
        run: ScheduleRun,
        event_type: str,
        decision: str,
        **details,
    ) -> None:
        self.audit.record(
            correlation_id=str(run.correlation_id or run.run_id),
            event_type=event_type,
            decision=decision,
            details={
                "schedule_id": str(run.schedule_id),
                "schedule_revision": run.schedule_revision,
                "run_id": str(run.run_id),
                "scheduled_for": run.scheduled_for.isoformat(),
                **details,
            },
        )
        schedule = self.store.get(run.schedule_id)
        if isinstance(schedule.trigger, OneTimeTrigger) and event_type in {
            "schedule_run_completed",
            "schedule_run_failed",
            "schedule_run_blocked",
            "schedule_approval_rejected",
            "schedule_approval_expired",
        }:
            schedule = self.store.retire_authorization(
                schedule.schedule_id,
                "One-time occurrence reached a terminal state",
            )
        if event_type == "schedule_run_failed":
            disabled = self.store.disable_after_consecutive_failures(
                schedule.schedule_id,
                schedule.run_policy.max_consecutive_failures,
            )
            if disabled is not None:
                schedule = disabled
        if (
            event_type == "schedule_run_completed"
            and self.notify_on_success
            and schedule.delivery_policy.on_success != "silent"
            and not isinstance(schedule.action, NotificationAction)
        ):
            self.notifications.publish(
                NotificationMessage(
                    title="Automation completed",
                    message=self._success_notification_message(schedule, run),
                    severity="success",
                    schedule_id=schedule.schedule_id,
                    run_id=run.run_id,
                )
            )
        elif (
            event_type in {"schedule_run_failed", "schedule_run_blocked"} and self.notify_on_failure
        ):
            self.notifications.publish(
                NotificationMessage(
                    title="Automation needs attention",
                    message=f"{schedule.name} did not complete. Review its run history.",
                    severity="error",
                    schedule_id=schedule.schedule_id,
                    run_id=run.run_id,
                )
            )
        if self.metrics is not None and event_type in {
            "schedule_run_completed",
            "schedule_run_failed",
            "schedule_run_blocked",
            "schedule_approval_rejected",
            "schedule_approval_expired",
        }:
            duration_ms = 0.0
            if run.started_at is not None:
                duration_ms = max(
                    0.0,
                    (self.clock.now() - run.started_at).total_seconds() * 1000,
                )
            self.metrics.record(
                correlation_id=str(run.correlation_id or run.run_id),
                stage="background_schedule",
                duration_ms=duration_ms,
                outcome=decision,
            )

    @staticmethod
    def _success_notification_message(
        schedule: ScheduleDefinition,
        run: ScheduleRun,
    ) -> str:
        fallback = f"{schedule.name} completed successfully."
        if not isinstance(run.result, dict):
            return fallback[:500]
        field = "display_text" if schedule.delivery_policy.on_success == "always" else "summary"
        value = run.result.get(field) or run.result.get("summary")
        if not isinstance(value, str) or not value.strip():
            return fallback[:500]
        value = value.strip()
        if len(value) <= 500:
            return value
        return value[:497].rstrip() + "..."
