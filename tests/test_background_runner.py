from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from assistant.bootstrap import build_container
from assistant.contracts import (
    PermissionContext,
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.policy.file_access import FileAccessPolicy
from assistant.policy.permissions import GrantType
from assistant.scheduling.authoring import ScheduleAuthoringService
from assistant.scheduling.contracts import (
    OneTimeTrigger,
    RecurrenceTrigger,
    ScheduleRunState,
    ScheduleStatus,
)
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.storage.macros import MacroDefinition, MacroStep


class _NoInput(BaseModel):
    pass


class _CountOutput(BaseModel):
    count: int


class _SensitiveOutput(BaseModel):
    count: int
    token: str


class _CriticalTestSkill(Skill):
    input_model = _NoInput
    output_model = _CountOutput
    manifest = manifest(
        skill_id="test.critical_background",
        display_name="Critical test action",
        description="Record one test execution.",
        input_model=input_model,
        output_model=output_model,
        risk=RiskLevel.CRITICAL,
    )

    def __init__(self) -> None:
        self.count = 0

    async def preview(self, call: SkillCall) -> str:
        return "Record one critical test execution"

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        self.count += 1
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Recorded.",
            data={"count": self.count},
        )


class _FailsOnceSkill(Skill):
    input_model = _NoInput
    output_model = _CountOutput
    manifest = manifest(
        skill_id="test.safe_retry",
        display_name="Safe retry test",
        description="Fail once and then succeed.",
        input_model=input_model,
        output_model=output_model,
        safe_to_retry=True,
    )

    def __init__(self) -> None:
        self.count = 0

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        self.count += 1
        if self.count == 1:
            raise RuntimeError("transient failure")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Recovered.",
            data={"count": self.count},
        )


class _UnsafeFailureSkill(_FailsOnceSkill):
    manifest = manifest(
        skill_id="test.unsafe_failure",
        display_name="Unsafe failure test",
        description="Fail without retrying.",
        input_model=_NoInput,
        output_model=_CountOutput,
        safe_to_retry=False,
    )


class _AlwaysFailsSkill(_UnsafeFailureSkill):
    manifest = manifest(
        skill_id="test.always_fails",
        display_name="Persistent failure test",
        description="Always fail for threshold testing.",
        input_model=_NoInput,
        output_model=_CountOutput,
    )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        self.count += 1
        raise RuntimeError("persistent failure")


class _InteractiveSessionSkill(_CriticalTestSkill):
    manifest = manifest(
        skill_id="test.interactive_session",
        display_name="Interactive session test",
        description="Requires the unlocked interactive desktop.",
        input_model=_NoInput,
        output_model=_CountOutput,
        permissions=(
            PermissionScope(
                capability="input.control",
                targets=("desktop",),
                reason="Use the interactive desktop.",
            ),
        ),
    )


class _SensitiveResultSkill(_CriticalTestSkill):
    output_model = _SensitiveOutput
    manifest = manifest(
        skill_id="test.sensitive_result",
        display_name="Sensitive result test",
        description="Return a sensitive test result.",
        input_model=_NoInput,
        output_model=output_model,
    )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        self.count += 1
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Sensitive result.",
            data={"count": self.count, "token": "must-not-persist"},
            sensitive=True,
        )


def _services(settings):
    container = build_container(settings.model_copy(update={"background_actions_enabled": True}))
    return container, ScheduleAuthoringService(
        container.schedule_store,
        container.registry,
        container.macro_store,
    )


def _due_now() -> OneTimeTrigger:
    return OneTimeTrigger(run_at=datetime.now(UTC))


class _AdjustableClock:
    def __init__(self) -> None:
        self.value = datetime.now(UTC)

    def now(self) -> datetime:
        return self.value


def test_authorized_low_risk_fixed_action_runs_through_engine(settings):
    container, authoring = _services(settings)
    schedule = authoring.create_fixed_action(
        name="Background calculation",
        trigger=_due_now(),
        skill_id="core.calculator",
        arguments={"expression": "6 * 7"},
    )

    assert asyncio.run(container.scheduler.run_once()) == 1

    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert run.state == ScheduleRunState.SUCCEEDED
    assert run.result is not None
    assert run.result["data"]["value"] == 42
    assert container.audit.query(event_type="skill_execution")[0]["skill_id"] == "core.calculator"
    assert container.audit.query(event_type="schedule_created")
    assert container.audit.query(event_type="schedule_authorized")
    assert container.audit.query(event_type="schedule_occurrence_created")
    assert container.audit.query(event_type="schedule_run_claimed")
    assert container.audit.query(event_type="schedule_run_started")
    assert container.audit.query(event_type="schedule_run_completed")
    assert container.schedule_store.get_authorization(schedule.schedule_id) is None


def test_creating_background_authority_requires_interactive_confirmation(settings):
    container, _ = _services(settings)
    call = SkillCall(
        skill_id="core.schedule",
        arguments={
            "operation": "create_action",
            "name": "Reviewed calculation",
            "skill_id": "core.calculator",
            "arguments": {"expression": "6 * 7"},
            "run_at": datetime.now(UTC),
        },
        permission_context=PermissionContext(session_id="interactive"),
    )

    prepared = asyncio.run(container.engine.prepare(call))

    assert prepared.confirmation is not None
    assert prepared.confirmation.risk == RiskLevel.MEDIUM
    assert container.schedule_store.list() == ()

    created = asyncio.run(
        container.engine.execute(
            call,
            confirmation_id=prepared.confirmation.confirmation_id,
        )
    )
    schedule_id = created.data["schedules"][0]["schedule_id"]
    assert created.status == ResultStatus.COMPLETED
    assert container.schedule_store.get(UUID(schedule_id)).status == ScheduleStatus.ACTIVE


def test_background_schedule_cannot_schedule_or_expand_its_own_authority(settings):
    _, authoring = _services(settings)

    with pytest.raises(ValueError, match="cannot be scheduled"):
        authoring.create_fixed_action(
            name="Recursive scheduler",
            trigger=_due_now(),
            skill_id="core.schedule",
            arguments={"operation": "list"},
        )

    with pytest.raises(ValueError, match="cannot persist secret field"):
        authoring.create_fixed_action(
            name="Embedded secret",
            trigger=_due_now(),
            skill_id="core.calculator",
            arguments={"expression": "2 + 2", "token": "secret"},
        )


def test_background_authorization_is_separate_from_interactive_grants(settings, tmp_path):
    container, authoring = _services(settings)
    root = tmp_path / "search"
    root.mkdir()
    (root / "needle.txt").write_text("test")
    schedule = authoring.create_fixed_action(
        name="Search approved folder",
        trigger=_due_now(),
        skill_id="windows.file_search",
        arguments={"root": str(root), "query": "needle"},
    )
    scheduled_call = schedule.action.call
    required = tuple(
        container.registry.get(scheduled_call.skill_id).required_permissions(scheduled_call)
    )

    assert container.permissions.authorize(scheduled_call, required).allowed
    changed_arguments = scheduled_call.model_copy(
        update={"arguments": {**scheduled_call.arguments, "query": "different"}}
    )
    changed_required = tuple(
        container.registry.get(changed_arguments.skill_id).required_permissions(changed_arguments)
    )
    assert not container.permissions.authorize(
        changed_arguments,
        changed_required,
    ).allowed
    interactive_call = SkillCall(
        skill_id="windows.file_search",
        arguments=scheduled_call.arguments,
        permission_context=PermissionContext(session_id="interactive"),
    )
    assert not container.permissions.authorize(interactive_call, required).allowed

    container.permissions.grant(
        user_id="local-user",
        skill_id="windows.file_search",
        capability="filesystem.read",
        grant_type=GrantType.ALWAYS,
        targets=(str(root),),
    )
    unrelated_background = scheduled_call.model_copy(
        update={
            "permission_context": scheduled_call.permission_context.model_copy(
                update={"schedule_id": uuid4()}
            )
        }
    )
    assert not container.permissions.authorize(unrelated_background, required).allowed


def test_high_risk_action_waits_for_approval_without_side_effect(settings, tmp_path):
    container, authoring = _services(settings)
    target = tmp_path / "keep.txt"
    target.write_text("keep")
    schedule = authoring.create_fixed_action(
        name="Delete later",
        trigger=_due_now(),
        skill_id="windows.file_operation",
        arguments={"operation": "delete", "source": str(target)},
    )

    assert asyncio.run(container.scheduler.run_once()) == 1

    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert run.state == ScheduleRunState.AWAITING_APPROVAL
    assert run.approval_expires_at is not None
    assert target.read_text() == "keep"
    assert container.notification_center.history()[0].run_id == run.run_id


def test_medium_risk_occurrence_can_be_reviewed_and_approved(settings, tmp_path):
    container, authoring = _services(settings)
    target = tmp_path / "approved.txt"
    schedule = authoring.create_fixed_action(
        name="Create reviewed file",
        trigger=_due_now(),
        skill_id="windows.file_operation",
        arguments={
            "operation": "create_text",
            "source": str(target),
            "content": "approved",
        },
    )
    asyncio.run(container.scheduler.run_once())
    pending = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]

    review = asyncio.run(container.scheduler.runner.prepare_approval(pending.run_id))
    item = review.confirmations[0]
    approved = asyncio.run(
        container.scheduler.runner.approve(
            pending.run_id,
            {item.step_id: item.request.confirmation_id},
        )
    )

    assert item.step_id == "action"
    assert str(target) in item.request.exact_targets
    assert approved.state == ScheduleRunState.SUCCEEDED
    assert target.read_text() == "approved"


def test_critical_occurrence_requires_typed_confirmation_token(settings):
    container = build_container(settings.model_copy(update={"background_actions_enabled": True}))
    skill = _CriticalTestSkill()
    container.registry.register(skill)
    authoring = ScheduleAuthoringService(
        container.schedule_store,
        container.registry,
        container.macro_store,
    )
    schedule = authoring.create_fixed_action(
        name="Critical review",
        trigger=_due_now(),
        skill_id=skill.manifest.skill_id,
        arguments={},
    )
    asyncio.run(container.scheduler.run_once())
    pending = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]

    review = asyncio.run(container.scheduler.runner.prepare_approval(pending.run_id))
    item = review.confirmations[0]
    approved = asyncio.run(
        container.scheduler.runner.approve(
            pending.run_id,
            {item.step_id: item.request.confirmation_id},
            typed_tokens={
                item.step_id: str(item.request.confirmation_id)[-6:],
            },
        )
    )

    assert item.request.requires_typed_token
    assert approved.state == ScheduleRunState.SUCCEEDED
    assert skill.count == 1


def test_pending_approval_can_be_rejected_or_expired(settings, tmp_path):
    container, authoring = _services(settings)
    first = tmp_path / "first.txt"
    rejected_schedule = authoring.create_fixed_action(
        name="Reject file",
        trigger=_due_now(),
        skill_id="windows.file_operation",
        arguments={"operation": "create_text", "source": str(first)},
    )
    asyncio.run(container.scheduler.run_once())
    rejected_pending = container.schedule_store.list_runs(
        schedule_id=rejected_schedule.schedule_id
    )[0]

    rejected = container.scheduler.runner.reject(rejected_pending.run_id)

    assert rejected.state == ScheduleRunState.REJECTED
    assert not first.exists()

    second = tmp_path / "second.txt"
    expired_schedule = authoring.create_fixed_action(
        name="Expire file",
        trigger=_due_now(),
        skill_id="windows.file_operation",
        arguments={"operation": "create_text", "source": str(second)},
    )
    asyncio.run(container.scheduler.run_once())
    expired_pending = container.schedule_store.list_runs(schedule_id=expired_schedule.schedule_id)[
        0
    ]
    container.schedule_store.transition_run(
        expired_pending.run_id,
        expected=(ScheduleRunState.AWAITING_APPROVAL,),
        state=ScheduleRunState.AWAITING_APPROVAL,
        correlation_id=expired_pending.correlation_id,
        approval_expires_at=datetime(2020, 1, 1, tzinfo=UTC),
    )

    assert asyncio.run(container.scheduler.run_once()) == 0
    expired = container.schedule_store.get_run(expired_pending.run_id)
    assert expired.state == ScheduleRunState.EXPIRED
    assert not second.exists()


def test_frozen_macro_uses_snapshot_not_later_macro_edits(settings):
    container, authoring = _services(settings)
    container.macro_store.save(
        MacroDefinition(
            name="math",
            description="Two calculations",
            steps=(
                MacroStep("core.calculator", {"expression": "2 + 3"}),
                MacroStep("core.calculator", {"expression": "4 * 5"}),
            ),
        )
    )
    schedule = authoring.create_macro(
        name="Frozen math",
        trigger=_due_now(),
        macro_name="math",
    )
    container.macro_store.save(
        MacroDefinition(
            name="math",
            description="Edited after scheduling",
            steps=(MacroStep("core.calculator", {"expression": "99"}),),
        )
    )

    assert asyncio.run(container.scheduler.run_once()) == 1

    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert run.state == ScheduleRunState.SUCCEEDED
    assert run.result is not None
    assert run.result["step-1"]["data"]["value"] == 5
    assert run.result["step-2"]["data"]["value"] == 20


def test_frozen_medium_risk_macro_requires_and_consumes_step_confirmations(
    settings,
    tmp_path,
):
    container, authoring = _services(settings)
    first = tmp_path / "first-approved.txt"
    second = tmp_path / "second-approved.txt"
    container.macro_store.save(
        MacroDefinition(
            name="create reviewed files",
            description="Two reviewed file creations",
            steps=(
                MacroStep(
                    "windows.file_operation",
                    {
                        "operation": "create_text",
                        "source": str(first),
                        "content": "first",
                    },
                ),
                MacroStep(
                    "windows.file_operation",
                    {
                        "operation": "create_text",
                        "source": str(second),
                        "content": "second",
                    },
                ),
            ),
        )
    )
    schedule = authoring.create_macro(
        name="Reviewed file macro",
        trigger=_due_now(),
        macro_name="create reviewed files",
    )
    asyncio.run(container.scheduler.run_once())
    pending = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]

    review = asyncio.run(container.scheduler.runner.prepare_approval(pending.run_id))
    confirmations = {item.step_id: item.request.confirmation_id for item in review.confirmations}
    approved = asyncio.run(container.scheduler.runner.approve(pending.run_id, confirmations))

    assert set(confirmations) == {"step-1", "step-2"}
    assert approved.state == ScheduleRunState.SUCCEEDED
    assert first.read_text() == "first"
    assert second.read_text() == "second"


def test_skill_drift_blocks_run_and_revokes_background_authorization(settings):
    container, authoring = _services(settings)
    schedule = authoring.create_fixed_action(
        name="Disabled calculation",
        trigger=_due_now(),
        skill_id="core.calculator",
        arguments={"expression": "1 + 1"},
    )
    container.registry.disable("core.calculator")

    assert asyncio.run(container.scheduler.run_once()) == 1

    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert run.state == ScheduleRunState.BLOCKED
    assert "unavailable" in (run.blocked_reason or "")
    assert container.schedule_store.get_authorization(schedule.schedule_id) is None
    assert (
        container.schedule_store.get(schedule.schedule_id).status == ScheduleStatus.PENDING_REVIEW
    )


def test_file_policy_change_blocks_authorized_background_target(settings, tmp_path):
    container, authoring = _services(settings)
    root = tmp_path / "later-blocked"
    root.mkdir()
    (root / "item.txt").write_text("item")
    schedule = authoring.create_fixed_action(
        name="Search later blocked folder",
        trigger=_due_now(),
        skill_id="windows.file_search",
        arguments={"root": str(root), "query": "item"},
    )
    container.file_access_policy.set(FileAccessPolicy(read_blacklist=(str(root),)))

    assert asyncio.run(container.scheduler.run_once()) == 1

    blocked = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert blocked.state == ScheduleRunState.BLOCKED
    assert blocked.result is not None
    assert blocked.result["error"]["code"] == "file_policy_denied"


def test_background_actions_global_switch_blocks_fixed_actions(settings):
    container = build_container(settings)
    authoring = ScheduleAuthoringService(
        container.schedule_store,
        container.registry,
        container.macro_store,
    )
    schedule = authoring.create_fixed_action(
        name="Disabled globally",
        trigger=_due_now(),
        skill_id="core.calculator",
        arguments={"expression": "3 + 4"},
    )

    assert asyncio.run(container.scheduler.run_once()) == 1

    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert run.state == ScheduleRunState.BLOCKED
    assert "disabled in settings" in (run.blocked_reason or "")


def test_authoring_rejects_interactive_desktop_background_action(settings):
    container = build_container(settings.model_copy(update={"background_actions_enabled": True}))
    container.scheduler.runner.session_unlocked = lambda: False
    skill = _InteractiveSessionSkill()
    container.registry.register(skill)
    authoring = ScheduleAuthoringService(
        container.schedule_store,
        container.registry,
        container.macro_store,
    )
    with pytest.raises(ValueError, match="interrupting the interactive desktop"):
        authoring.create_fixed_action(
            name="Needs desktop",
            trigger=_due_now(),
            skill_id=skill.manifest.skill_id,
            arguments={},
        )

    assert container.schedule_store.list() == ()
    assert skill.count == 0


def test_sensitive_background_result_is_redacted_before_persistence(settings):
    container = build_container(settings.model_copy(update={"background_actions_enabled": True}))
    skill = _SensitiveResultSkill()
    container.registry.register(skill)
    authoring = ScheduleAuthoringService(
        container.schedule_store,
        container.registry,
        container.macro_store,
    )
    schedule = authoring.create_fixed_action(
        name="Redact result",
        trigger=_due_now(),
        skill_id=skill.manifest.skill_id,
        arguments={},
    )

    assert asyncio.run(container.scheduler.run_once()) == 1

    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert run.state == ScheduleRunState.SUCCEEDED
    assert run.result["data"] == {"redacted": True}
    assert "must-not-persist" not in str(run.result)


def test_safe_background_failure_uses_bounded_durable_retry(settings):
    container = build_container(settings.model_copy(update={"background_actions_enabled": True}))
    clock = _AdjustableClock()
    container.schedule_store.clock = clock
    container.scheduler.clock = clock
    container.scheduler.runner.clock = clock
    skill = _FailsOnceSkill()
    container.registry.register(skill)
    authoring = ScheduleAuthoringService(
        container.schedule_store,
        container.registry,
        container.macro_store,
    )
    schedule = authoring.create_fixed_action(
        name="Retry safely",
        trigger=OneTimeTrigger(run_at=clock.now()),
        skill_id=skill.manifest.skill_id,
        arguments={},
    )
    schedule = container.schedule_store.revise(
        schedule.schedule_id,
        run_policy=schedule.run_policy.model_copy(update={"max_attempts": 2}),
        authorize=True,
    )

    assert asyncio.run(container.scheduler.run_once()) == 1
    pending = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert pending.state == ScheduleRunState.PENDING
    assert pending.attempt == 1
    assert pending.retry_at is not None

    clock.value = pending.retry_at
    assert asyncio.run(container.scheduler.run_once()) == 1
    completed = container.schedule_store.get_run(pending.run_id)
    assert completed.state == ScheduleRunState.SUCCEEDED
    assert completed.attempt == 2
    assert skill.count == 2


def test_unsafe_background_failure_is_never_retried(settings):
    container = build_container(settings.model_copy(update={"background_actions_enabled": True}))
    skill = _UnsafeFailureSkill()
    container.registry.register(skill)
    authoring = ScheduleAuthoringService(
        container.schedule_store,
        container.registry,
        container.macro_store,
    )
    schedule = authoring.create_fixed_action(
        name="Do not retry",
        trigger=_due_now(),
        skill_id=skill.manifest.skill_id,
        arguments={},
    )
    schedule = container.schedule_store.revise(
        schedule.schedule_id,
        run_policy=schedule.run_policy.model_copy(update={"max_attempts": 3}),
        authorize=True,
    )

    assert asyncio.run(container.scheduler.run_once()) == 1

    failed = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert failed.state == ScheduleRunState.FAILED
    assert failed.attempt == 1
    assert failed.retry_at is None
    assert skill.count == 1


def test_repeated_failures_disable_schedule_and_revoke_authority(settings):
    container = build_container(settings.model_copy(update={"background_actions_enabled": True}))
    skill = _AlwaysFailsSkill()
    container.registry.register(skill)
    authoring = ScheduleAuthoringService(
        container.schedule_store,
        container.registry,
        container.macro_store,
    )
    schedule = authoring.create_fixed_action(
        name="Failure threshold",
        trigger=RecurrenceTrigger(
            frequency="hours",
            interval=1,
            start_at=datetime.now(UTC),
            timezone="UTC",
        ),
        skill_id=skill.manifest.skill_id,
        arguments={},
    )
    schedule = container.schedule_store.revise(
        schedule.schedule_id,
        run_policy=schedule.run_policy.model_copy(update={"max_consecutive_failures": 2}),
        authorize=True,
    )

    for _ in range(2):
        container.schedule_store.run_now(schedule.schedule_id)
        assert asyncio.run(container.scheduler.run_once()) == 1

    disabled = container.schedule_store.get(schedule.schedule_id)
    assert disabled.status == ScheduleStatus.DISABLED
    assert container.schedule_store.get_authorization(schedule.schedule_id) is None
    assert skill.count == 2


def test_hourly_run_budget_blocks_excess_manual_occurrences(settings):
    container, authoring = _services(settings)
    schedule = authoring.create_fixed_action(
        name="Rate bounded calculation",
        trigger=RecurrenceTrigger(
            frequency="hours",
            interval=1,
            start_at=datetime.now(UTC),
            timezone="UTC",
        ),
        skill_id="core.calculator",
        arguments={"expression": "2 + 2"},
    )
    for _ in range(5):
        container.schedule_store.run_now(schedule.schedule_id)

    assert asyncio.run(container.scheduler.run_once()) == 5

    runs = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)
    assert sum(run.state == ScheduleRunState.SUCCEEDED for run in runs) == 4
    assert sum(run.state == ScheduleRunState.BLOCKED for run in runs) == 1
    assert any("Hourly" in (run.blocked_reason or "") for run in runs)
