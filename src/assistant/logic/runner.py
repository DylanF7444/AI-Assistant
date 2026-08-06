from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from assistant.contracts import (
    RequestResultItem,
    ResultStatus,
    SkillCall,
    SkillError,
    SkillResult,
)
from assistant.execution.engine import ExecutionEngine
from assistant.logic.contracts import ChoiceOption, LogicWorkflow
from assistant.logic.evaluator import (
    choose_option,
    evaluate_predicate,
    options_from_items,
    render_template,
)
from assistant.logic.store import LogicStateStore, WorkflowProgress
from assistant.scheduling.clock import Clock
from assistant.scheduling.contracts import ScheduleDefinition, ScheduleRun
from assistant.scheduling.notifications import NotificationCenter, NotificationMessage


@dataclass(frozen=True)
class LogicRunOutcome:
    result: SkillResult
    progress: WorkflowProgress | None = None
    condition_met: bool = False
    action_executed: bool = False
    choice_id: str | None = None


class LogicWorkflowRunner:
    def __init__(
        self,
        engine: ExecutionEngine,
        state: LogicStateStore,
        notifications: NotificationCenter,
        clock: Clock,
    ) -> None:
        self.engine = engine
        self.state = state
        self.notifications = notifications
        self.clock = clock

    async def run(
        self,
        schedule: ScheduleDefinition,
        run: ScheduleRun,
        workflow: LogicWorkflow,
        calls: Mapping[str, SkillCall],
        *,
        confirmations: Mapping[str, tuple[UUID, str | None]] | None = None,
    ) -> LogicRunOutcome:
        progress = self.state.workflow_progress(schedule.schedule_id, schedule.revision)
        if progress.should_stop:
            result = SkillResult(
                status=ResultStatus.COMPLETED,
                summary="The workflow was already complete; no action ran.",
                data={"condition_met": False, "action_executed": False},
            )
            return LogicRunOutcome(
                result=self._with_progress(result, progress),
                progress=progress,
            )
        if (
            workflow.completion.expires_at is not None
            and workflow.completion.expires_at <= self.clock.now()
        ):
            return LogicRunOutcome(
                result=SkillResult(
                    status=ResultStatus.COMPLETED,
                    summary="The workflow expired; no action ran.",
                    data={"condition_met": False, "action_executed": False},
                )
            )

        approvals = dict(confirmations or {})
        check_result = None
        condition_met = True
        if workflow.check is not None:
            check_result = await self._execute_call(
                calls["logic-check"],
                approvals.get("logic-check"),
            )
            if check_result.status != ResultStatus.COMPLETED:
                return LogicRunOutcome(result=check_result)
            if workflow.when is not None:
                condition_met = evaluate_predicate(workflow.when, check_result)

        choice = None
        if condition_met and workflow.selection is not None:
            try:
                choice = self._select(
                    schedule,
                    run,
                    workflow,
                    check_result,
                )
            except ValueError as exc:
                return LogicRunOutcome(
                    result=SkillResult(
                        status=ResultStatus.FAILED,
                        summary=f"Workflow selection failed: {exc}",
                        error=SkillError(
                            code="logic_selection_failed",
                            message=str(exc),
                            recoverable=True,
                        ),
                    ),
                    condition_met=condition_met,
                )

        if not condition_met:
            result = SkillResult(
                status=ResultStatus.COMPLETED,
                summary="The workflow condition was not met; no action ran.",
                data={"condition_met": False, "action_executed": False},
            )
            return LogicRunOutcome(result=result)

        result = await self._run_action(
            schedule,
            run,
            workflow,
            calls,
            choice,
            check_result,
            approvals,
        )
        return LogicRunOutcome(
            result=result,
            condition_met=True,
            action_executed=bool(result.data.get("action_executed", True)),
            choice_id=choice.option_id if choice else None,
        )

    def record(
        self,
        schedule: ScheduleDefinition,
        workflow: LogicWorkflow,
        outcome: LogicRunOutcome,
    ) -> LogicRunOutcome:
        if outcome.progress is not None:
            return outcome
        progress = self.state.record_workflow_run(
            schedule_id=schedule.schedule_id,
            revision=schedule.revision,
            completion=workflow.completion,
            succeeded=outcome.result.status == ResultStatus.COMPLETED
            and outcome.action_executed,
            condition_met=outcome.condition_met,
            choice_id=outcome.choice_id,
            observed_at=self.clock.now(),
        )
        return LogicRunOutcome(
            result=self._with_progress(outcome.result, progress),
            progress=progress,
            condition_met=outcome.condition_met,
            action_executed=outcome.action_executed,
            choice_id=outcome.choice_id,
        )

    async def _run_action(
        self,
        schedule: ScheduleDefinition,
        run: ScheduleRun,
        workflow: LogicWorkflow,
        calls: Mapping[str, SkillCall],
        choice: ChoiceOption | None,
        check_result: SkillResult | None,
        approvals: Mapping[str, tuple[UUID, str | None]],
    ) -> SkillResult:
        if choice is not None and choice.action is not None:
            step_id = f"logic-choice-{choice.option_id}"
            action_result = await self._execute_call(calls[step_id], approvals.get(step_id))
            return self._wrapped_action_result(action_result, choice)
        if (
            choice is not None
            and workflow.selection is not None
            and any(option.action is not None for option in workflow.selection.options)
        ):
            return SkillResult(
                status=ResultStatus.COMPLETED,
                summary=f"Selected {choice.label}; this branch intentionally took no action.",
                data={
                    "condition_met": True,
                    "action_executed": False,
                    "selected_id": choice.option_id,
                    "selected_value": choice.value,
                },
                items=self._choice_items(choice),
            )

        action = workflow.action
        if action.kind == "skill":
            action_result = await self._execute_call(
                calls["logic-action"],
                approvals.get("logic-action"),
            )
            return self._wrapped_action_result(action_result, choice)
        if action.kind == "notification":
            title = render_template(
                action.title,
                choice=choice,
                check=check_result,
                max_length=100,
            )
            message = render_template(
                action.message,
                choice=choice,
                check=check_result,
                max_length=500,
            )
            self.notifications.publish(
                NotificationMessage(
                    title=title,
                    message=message,
                    severity="info",
                    category="automation",
                    schedule_id=schedule.schedule_id,
                    run_id=run.run_id,
                )
            )
            return SkillResult(
                status=ResultStatus.COMPLETED,
                summary=message,
                display_text=message,
                data={
                    "condition_met": True,
                    "action_executed": True,
                    "notification_delivered": True,
                    "selected_id": choice.option_id if choice else None,
                },
                items=self._choice_items(choice),
            )
        assert choice is not None
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Selected {choice.label}.",
            display_text=f"Selected: {choice.label}",
            data={
                "condition_met": True,
                "action_executed": True,
                "selected_id": choice.option_id,
                "selected_value": choice.value,
            },
            items=self._choice_items(choice),
        )

    def _select(
        self,
        schedule: ScheduleDefinition,
        run: ScheduleRun,
        workflow: LogicWorkflow,
        check_result: SkillResult | None,
    ) -> ChoiceOption:
        assert workflow.selection is not None
        checkpoint_key = f"logic:{run.idempotency_key}:selection"
        dynamic = options_from_items(
            None if check_result is None or check_result.sensitive else check_result.items
        )
        options = workflow.selection.options if workflow.selection.source == "static" else dynamic
        existing = self.state.checkpoint_choice(checkpoint_key)
        if existing:
            selected = next((option for option in options if option.option_id == existing), None)
            if selected is None:
                raise ValueError("The checkpointed choice is no longer available")
            return selected
        progress = self.state.workflow_progress(schedule.schedule_id, schedule.revision)
        selected = choose_option(
            workflow.selection,
            selection_key=checkpoint_key,
            recent_ids=progress.recent_choice_ids,
            dynamic_options=dynamic,
        )
        history_key = (
            f"schedule:{schedule.schedule_id}:{schedule.revision}:"
            f"{workflow.selection.history_key or 'selection'}"
        )
        reserved = self.state.reserve_choice(
            checkpoint_key=checkpoint_key,
            option_id=selected.option_id,
            history_key=history_key,
        )
        return next(option for option in options if option.option_id == reserved)

    async def _execute_call(
        self,
        call: SkillCall,
        approval: tuple[UUID, str | None] | None,
    ) -> SkillResult:
        confirmation_id, typed_token = approval or (None, None)
        return await self.engine.execute(
            call,
            confirmation_id=confirmation_id,
            typed_token=typed_token,
        )

    @staticmethod
    def _wrapped_action_result(result: SkillResult, choice: ChoiceOption | None) -> SkillResult:
        return SkillResult(
            status=result.status,
            summary=result.summary,
            display_text=result.display_text,
            speech_text=result.speech_text,
            media_directive=result.media_directive,
            visual_artifact=result.visual_artifact,
            sensitive=result.sensitive,
            items=result.items or LogicWorkflowRunner._choice_items(choice),
            data={
                "condition_met": True,
                "action_executed": True,
                "selected_id": choice.option_id if choice else None,
                "action_result": result.model_dump(mode="json"),
            },
            error=result.error,
            rollback_token=result.rollback_token,
        )

    @staticmethod
    def _with_progress(result: SkillResult, progress: WorkflowProgress) -> SkillResult:
        data = dict(result.data)
        data["workflow"] = {
            "run_count": progress.run_count,
            "success_count": progress.success_count,
            "condition_true_count": progress.condition_true_count,
            "stopped": progress.should_stop,
            "stop_reason": progress.stop_reason,
        }
        return result.model_copy(update={"data": data})

    @staticmethod
    def _choice_items(choice: ChoiceOption | None) -> tuple[RequestResultItem, ...] | None:
        if choice is None:
            return None
        return (
            RequestResultItem(
                item_id=choice.option_id,
                title=choice.label,
                value=choice.value,
                kind="choice",
                metadata=choice.metadata,
            ),
        )
