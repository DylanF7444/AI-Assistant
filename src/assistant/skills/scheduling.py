from __future__ import annotations

import asyncio
from datetime import datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from assistant.contracts import ResultStatus, RiskLevel, SkillCall, SkillResult
from assistant.scheduling.authoring import ScheduleAuthoringService
from assistant.scheduling.contracts import (
    OneTimeTrigger,
    RecurrenceTrigger,
    ScheduledTrigger,
    ScheduleStatus,
)
from assistant.scheduling.store import ScheduleStore
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest


class RecurrenceInput(BaseModel):
    frequency: Literal["minutes", "hours", "daily", "weekly", "monthly"]
    interval: int = Field(default=1, ge=1, le=10_000)
    local_time: time | None = None
    weekdays: tuple[int, ...] = ()
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    timezone: str | None = None
    end_at: datetime | None = None


class ScheduleManagementInput(BaseModel):
    operation: Literal[
        "create",
        "create_action",
        "create_macro",
        "list",
        "get",
        "pause",
        "resume",
        "update",
        "delete",
        "run_now",
        "history",
        "clear_history",
        "snooze",
    ]
    schedule: str | None = None
    name: str | None = None
    message: str | None = None
    recurrence: RecurrenceInput | None = None
    run_at: datetime | None = None
    delay_seconds: float | None = Field(default=None, gt=0, le=31_536_000)
    skill_id: str | None = None
    arguments: dict = Field(default_factory=dict)
    macro_name: str | None = None

    @model_validator(mode="after")
    def _operation_has_required_fields(self) -> ScheduleManagementInput:
        if self.operation == "create" and (
            not self.name or not self.message or self.recurrence is None
        ):
            raise ValueError("Creating a schedule requires name, message, and recurrence")
        if self.operation == "create_action" and (
            not self.name or not self.skill_id or (self.recurrence is None) == (self.run_at is None)
        ):
            raise ValueError(
                "Creating an action schedule requires name, skill_id, and exactly one trigger"
            )
        if self.operation == "create_macro" and (
            not self.name
            or not self.macro_name
            or (self.recurrence is None) == (self.run_at is None)
        ):
            raise ValueError(
                "Creating a macro schedule requires name, macro_name, and exactly one trigger"
            )
        if (
            self.operation
            in {
                "get",
                "pause",
                "resume",
                "update",
                "delete",
                "run_now",
                "history",
                "clear_history",
                "snooze",
            }
            and not self.schedule
        ):
            raise ValueError(f"{self.operation} requires a schedule name or ID")
        if self.operation == "snooze" and self.delay_seconds is None:
            raise ValueError("Snooze requires delay_seconds")
        if self.operation == "update" and not any((self.name, self.message, self.recurrence)):
            raise ValueError("Update requires a changed name, message, or recurrence")
        return self


class ScheduleManagementOutput(BaseModel):
    schedules: list[dict]
    runs: list[dict] = Field(default_factory=list)


class ScheduleSkill(Skill):
    input_model = ScheduleManagementInput
    output_model = ScheduleManagementOutput

    def __init__(
        self,
        store: ScheduleStore,
        default_timezone: str = "UTC",
        authoring: ScheduleAuthoringService | None = None,
    ) -> None:
        self.store = store
        self.default_timezone = default_timezone
        self.authoring = authoring
        self.manifest = manifest(
            skill_id="core.schedule",
            display_name="Schedules",
            description=(
                "Create recurring reminders and list, inspect, pause, resume, update, "
                "delete, snooze, or run configured schedules."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "create":
            assert args.name and args.recurrence
            return f"Create recurring schedule {args.name}: {args.recurrence.model_dump()}"
        if args.operation == "create_action":
            if self.authoring is None:
                raise ValueError("Background action authoring is unavailable")
            assert args.name and args.skill_id
            action = await self.authoring.preview_action(args.skill_id, args.arguments)
            return f"Create schedule {args.name}: {self._trigger_value(args)}; action: {action}"
        if args.operation == "create_macro":
            assert args.name and args.macro_name
            return (
                f"Create schedule {args.name}: {self._trigger_value(args)}; "
                f"frozen macro: {args.macro_name}"
            )
        return f"{args.operation.replace('_', ' ').title()} schedule {args.schedule or ''}".strip()

    def risk_for(self, call: SkillCall) -> RiskLevel:
        args = self.validate(call.arguments)
        if self.authoring is None:
            return RiskLevel.LOW
        if args.operation == "create_action" and args.skill_id:
            return self._authorization_risk(
                self.authoring.risk_for_action(args.skill_id, args.arguments)
            )
        if args.operation == "create_macro" and args.macro_name:
            return self._authorization_risk(self.authoring.risk_for_macro(args.macro_name))
        return RiskLevel.LOW

    @staticmethod
    def _authorization_risk(action_risk: RiskLevel) -> RiskLevel:
        # Creating standing background authority always requires an explicit
        # interactive confirmation, even when the frozen action itself is low risk.
        return RiskLevel.MEDIUM if action_risk == RiskLevel.LOW else action_risk

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="Schedule action cancelled.")
        changed = None
        runs = []
        if args.operation == "create":
            assert args.name and args.message and args.recurrence
            changed = self.store.create_recurring_notification(
                name=args.name,
                message=args.message,
                trigger=self._trigger(args.recurrence),
            )
        elif args.operation == "create_action":
            if self.authoring is None:
                raise ValueError("Background action authoring is unavailable")
            assert args.name and args.skill_id
            changed = self.authoring.create_fixed_action(
                name=args.name,
                trigger=self._trigger_value(args),
                skill_id=args.skill_id,
                arguments=args.arguments,
            )
        elif args.operation == "create_macro":
            if self.authoring is None:
                raise ValueError("Background action authoring is unavailable")
            assert args.name and args.macro_name
            changed = self.authoring.create_macro(
                name=args.name,
                trigger=self._trigger_value(args),
                macro_name=args.macro_name,
            )
        elif args.operation == "list":
            pass
        else:
            schedule = self._resolve(args.schedule or "")
            if args.operation == "get":
                changed = schedule
            elif args.operation == "pause":
                changed = self.store.pause(schedule.schedule_id)
            elif args.operation == "resume":
                changed = self.store.resume(schedule.schedule_id)
            elif args.operation == "delete":
                changed = self.store.delete(schedule.schedule_id)
            elif args.operation == "run_now":
                run = self.store.run_now(schedule.schedule_id)
                changed = schedule
                runs = [self._run_value(run)]
            elif args.operation == "history":
                changed = schedule
                runs = [
                    self._run_value(run)
                    for run in self.store.list_runs(schedule_id=schedule.schedule_id)
                ]
            elif args.operation == "clear_history":
                self.store.clear_completed_run_history(schedule.schedule_id)
                changed = schedule
            elif args.operation == "snooze":
                assert args.delay_seconds is not None
                changed = self.store.snooze(schedule.schedule_id, args.delay_seconds)
            elif args.operation == "update":
                if args.recurrence is not None or args.message is not None:
                    action = schedule.action
                    if args.message is not None:
                        if action.kind != "notify_only":
                            raise ValueError("Only notification schedule messages can be edited")
                        action = action.model_copy(update={"message": args.message})
                    changed = self.store.revise(
                        schedule.schedule_id,
                        trigger=(self._trigger(args.recurrence) if args.recurrence else None),
                        action=action,
                        authorize=True,
                    )
                else:
                    changed = schedule
                if args.name is not None:
                    changed = self.store.rename(changed.schedule_id, name=args.name)

        schedules = (
            self.store.list(
                statuses=tuple(
                    status for status in ScheduleStatus if status != ScheduleStatus.DELETED
                )
            )
            if args.operation == "list"
            else (changed,)
            if changed is not None
            else ()
        )
        values = [self._schedule_value(schedule) for schedule in schedules]
        summary = self._summary(args.operation, changed, len(values), len(runs))
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            display_text=self._display(values, runs, summary),
            data=ScheduleManagementOutput(schedules=values, runs=runs).model_dump(mode="json"),
        )

    def _resolve(self, reference: str):
        try:
            schedule_id = UUID(reference)
        except ValueError:
            schedule_id = None
        if schedule_id is not None:
            try:
                return self.store.get(schedule_id)
            except KeyError:
                pass
        matches = [
            schedule
            for schedule in self.store.list()
            if schedule.name.casefold() == reference.strip().casefold()
            and schedule.status != ScheduleStatus.DELETED
        ]
        if not matches:
            raise ValueError(f"Unknown schedule: {reference}")
        if len(matches) > 1:
            raise ValueError(f"More than one schedule is named {reference}; use its ID")
        return matches[0]

    def _trigger(self, value: RecurrenceInput) -> RecurrenceTrigger:
        zone_name = value.timezone or self.default_timezone
        zone = ZoneInfo(zone_name)
        now = self.store.clock.now().astimezone(zone)
        return RecurrenceTrigger(
            frequency=value.frequency,
            interval=value.interval,
            local_time=value.local_time,
            weekdays=value.weekdays,
            day_of_month=value.day_of_month,
            start_at=now,
            end_at=value.end_at,
            timezone=zone_name,
        )

    def _trigger_value(self, args: ScheduleManagementInput) -> ScheduledTrigger:
        if args.run_at is not None:
            return OneTimeTrigger(run_at=args.run_at)
        assert args.recurrence is not None
        return self._trigger(args.recurrence)

    @staticmethod
    def _schedule_value(schedule) -> dict:
        return {
            "schedule_id": str(schedule.schedule_id),
            "name": schedule.name,
            "status": schedule.status.value,
            "revision": schedule.revision,
            "trigger": schedule.trigger.model_dump(mode="json"),
            "action": schedule.action.model_dump(mode="json"),
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
            "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        }

    @staticmethod
    def _run_value(run) -> dict:
        return {
            "run_id": str(run.run_id),
            "scheduled_for": run.scheduled_for.isoformat(),
            "state": run.state.value,
            "result_status": run.result_status,
            "blocked_reason": run.blocked_reason,
        }

    @staticmethod
    def _summary(operation: str, schedule, schedule_count: int, run_count: int) -> str:
        if operation == "list":
            return f"Found {schedule_count} configured schedules."
        if operation == "history":
            return f"Found {run_count} runs for {schedule.name}."
        if operation == "run_now":
            return f"Queued {schedule.name} to run now."
        verbs = {
            "create": "Created",
            "create_action": "Created",
            "create_macro": "Created",
            "get": "Found",
            "pause": "Paused",
            "resume": "Resumed",
            "update": "Updated",
            "delete": "Deleted",
            "clear_history": "Cleared completed history for",
            "snooze": "Snoozed",
        }
        return f"{verbs.get(operation, operation.title())} schedule {schedule.name}."

    @staticmethod
    def _display(schedules: list[dict], runs: list[dict], summary: str) -> str:
        lines = [summary]
        for schedule in schedules:
            due = schedule["next_run_at"] or "no future run"
            lines.append(
                f"- {schedule['name']} [{schedule['status']}] — next: {due} "
                f"(ID {schedule['schedule_id']})"
            )
        for run in runs:
            lines.append(f"- Run {run['run_id']}: {run['state']} at {run['scheduled_for']}")
        return "\n".join(lines)
