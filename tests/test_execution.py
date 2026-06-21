from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from assistant.contracts import (
    ExecutionPlan,
    PermissionContext,
    PermissionScope,
    PlanStep,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.events import EventBus
from assistant.execution.engine import ExecutionEngine
from assistant.policy.confirmations import ConfirmationManager
from assistant.policy.permissions import GrantType, PermissionManager
from assistant.skills.base import Skill
from assistant.skills.builtin import CalculatorSkill
from assistant.skills.builtin import FileSearchSkill
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import AuditStore, Database
from assistant.windows.adapters import LocalFileSystemAdapter


class ActionInput(BaseModel):
    target: str


class ActionOutput(BaseModel):
    count: int


class CountingSkill(Skill):
    input_model = ActionInput
    output_model = ActionOutput

    def __init__(self, risk=RiskLevel.MEDIUM):
        from assistant.skills.builtin import manifest

        self.count = 0
        self.manifest = manifest(
            skill_id="test.count",
            display_name="Counting",
            description="Count executions",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(capability="test.run", reason="Run the test action"),
            ),
            risk=risk,
        )

    async def execute(self, call, cancel_event):
        self.count += 1
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Counted.",
            data={"count": self.count},
        )


class FlakySkill(CountingSkill):
    def __init__(self, safe_to_retry):
        super().__init__(risk=RiskLevel.LOW)
        self.manifest = self.manifest.model_copy(
            update={
                "skill_id": "test.flaky",
                "safe_to_retry": safe_to_retry,
                "risk": RiskLevel.LOW,
                "permissions": (),
            }
        )

    async def execute(self, call, cancel_event):
        self.count += 1
        if self.count == 1:
            raise RuntimeError("temporary")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Recovered.",
            data={"count": self.count},
        )


class SlowSkill(CountingSkill):
    def __init__(self):
        super().__init__(risk=RiskLevel.LOW)
        self.manifest = self.manifest.model_copy(
            update={
                "skill_id": "test.slow",
                "risk": RiskLevel.LOW,
                "permissions": (),
            }
        )

    async def execute(self, call, cancel_event):
        await asyncio.sleep(5)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Late.",
            data={"count": 1},
        )


def engine_for(tmp_path: Path, *skills: Skill):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    audit = AuditStore(database)
    permissions = PermissionManager(database, audit)
    registry = SkillRegistry()
    for skill in skills:
        registry.register(skill)
    confirmations = ConfirmationManager()
    engine = ExecutionEngine(
        registry, permissions, confirmations, audit, EventBus()
    )
    return engine, permissions, audit


def engine_for_with_confirmation_overrides(
    tmp_path: Path, overrides: dict[str, bool], *skills: Skill
):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    audit = AuditStore(database)
    permissions = PermissionManager(database, audit)
    registry = SkillRegistry()
    for skill in skills:
        registry.register(skill)
    confirmations = ConfirmationManager()
    engine = ExecutionEngine(
        registry,
        permissions,
        confirmations,
        audit,
        EventBus(),
        confirmation_overrides=overrides,
    )
    return engine, permissions, audit


def make_call(skill_id, arguments=None, key="key"):
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments or {},
        permission_context=PermissionContext(session_id="session"),
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_invalid_arguments_are_rejected(tmp_path):
    engine, _, _ = engine_for(tmp_path, CalculatorSkill())
    result = await engine.execute(
        make_call("core.calculator", {"expression": "__import__('os').system('x')"})
    )
    assert result.status == ResultStatus.FAILED
    assert result.error.code == "skill_failed"


@pytest.mark.asyncio
async def test_permission_confirmation_and_idempotency(tmp_path):
    skill = CountingSkill()
    engine, permissions, audit = engine_for(tmp_path, skill)
    call = make_call("test.count", {"target": "item"})
    denied = await engine.prepare(call)
    assert denied.result.status == ResultStatus.DENIED
    assert denied.call == call
    assert denied.permission_requests[0].call.skill_id == "test.count"
    assert denied.permission_requests[0].scope.capability == "test.run"
    permissions.grant(
        user_id="local-user",
        skill_id="test.count",
        capability="test.run",
        grant_type=GrantType.ALWAYS,
    )
    prepared = await engine.prepare(call)
    assert prepared.confirmation is not None
    result = await engine.execute(
        call, confirmation_id=prepared.confirmation.confirmation_id
    )
    assert result.status == ResultStatus.COMPLETED
    duplicate = await engine.execute(call)
    assert duplicate == result
    assert skill.count == 1
    assert audit.query(event_type="skill_execution")


@pytest.mark.asyncio
async def test_missing_permission_publishes_approval_event(tmp_path):
    skill = CountingSkill(risk=RiskLevel.LOW)
    engine, _, _ = engine_for(tmp_path, skill)
    call = make_call("test.count", {"target": "item"})

    outcome = await engine.prepare(call)

    event = engine.events.history(call.correlation_id)[-1]
    assert outcome.permission_requests
    assert event.state.value == "awaiting_approval"
    assert event.data["permissions"][0]["skill_id"] == "test.count"
    assert event.data["permissions"][0]["capability"] == "test.run"


@pytest.mark.asyncio
async def test_one_time_permission_survives_prepare_until_execution(tmp_path):
    skill = CountingSkill(risk=RiskLevel.LOW)
    engine, permissions, _ = engine_for(tmp_path, skill)
    call = make_call("test.count", {"target": "item"})
    permissions.grant(
        user_id="local-user",
        skill_id="test.count",
        capability="test.run",
        grant_type=GrantType.ONCE,
    )
    prepared = await engine.prepare(call)
    assert prepared.result.status == ResultStatus.COMPLETED
    assert skill.count == 1
    assert not permissions.authorize(
        call, skill.required_permissions(call), consume_once=False
    ).allowed


@pytest.mark.asyncio
async def test_confirmation_rejects_changed_arguments(tmp_path):
    skill = CountingSkill()
    engine, permissions, _ = engine_for(tmp_path, skill)
    permissions.grant(
        user_id="local-user",
        skill_id="test.count",
        capability="test.run",
        grant_type=GrantType.ALWAYS,
    )
    call = make_call("test.count", {"target": "a"})
    prepared = await engine.prepare(call)
    changed = call.model_copy(update={"arguments": {"target": "b"}})
    result = await engine.execute(
        changed, confirmation_id=prepared.confirmation.confirmation_id
    )
    assert result.status == ResultStatus.DENIED
    assert skill.count == 0


@pytest.mark.asyncio
async def test_confirmation_can_be_disabled_for_specific_action(tmp_path):
    skill = CountingSkill()
    engine, permissions, _ = engine_for_with_confirmation_overrides(
        tmp_path, {"skill:test.count": False}, skill
    )
    permissions.grant(
        user_id="local-user",
        skill_id="test.count",
        capability="test.run",
        grant_type=GrantType.ALWAYS,
    )

    call = make_call("test.count", {"target": "a"})
    prepared = await engine.prepare(call)

    assert prepared.confirmation is None
    assert prepared.result.status == ResultStatus.COMPLETED
    assert skill.count == 1


@pytest.mark.asyncio
async def test_confirmation_can_be_enabled_for_low_risk_actions(tmp_path):
    skill = CountingSkill(risk=RiskLevel.LOW)
    engine, permissions, _ = engine_for_with_confirmation_overrides(
        tmp_path, {"risk:low": True}, skill
    )
    permissions.grant(
        user_id="local-user",
        skill_id="test.count",
        capability="test.run",
        grant_type=GrantType.ALWAYS,
    )

    prepared = await engine.prepare(make_call("test.count", {"target": "a"}))

    assert prepared.confirmation is not None
    assert skill.count == 0


@pytest.mark.asyncio
async def test_plan_stops_after_failed_prerequisite(tmp_path):
    engine, _, _ = engine_for(tmp_path, CalculatorSkill())
    bad = make_call("missing.skill", key="bad")
    dependent = make_call(
        "core.calculator", {"expression": "2 + 2"}, key="dependent"
    )
    plan = ExecutionPlan(
        steps=(
            PlanStep(step_id="bad", call=bad),
            PlanStep(step_id="dependent", call=dependent, depends_on=("bad",)),
        )
    )
    results = await engine.execute_plan(plan)
    assert results["bad"].status == ResultStatus.DENIED
    assert results["dependent"].status == ResultStatus.CANCELLED


@pytest.mark.asyncio
async def test_dynamic_filesystem_scope_blocks_other_root(tmp_path):
    approved = tmp_path / "approved"
    other = tmp_path / "other"
    approved.mkdir()
    other.mkdir()
    skill = FileSearchSkill(LocalFileSystemAdapter())
    engine, permissions, _ = engine_for(tmp_path, skill)
    permissions.grant(
        user_id="local-user",
        skill_id="windows.file_search",
        capability="filesystem.read",
        grant_type=GrantType.ALWAYS,
        targets=(str(approved),),
    )
    allowed = await engine.execute(
        make_call(
            "windows.file_search",
            {"root": str(approved), "query": "x", "limit": 5},
            key="allowed",
        )
    )
    denied = await engine.execute(
        make_call(
            "windows.file_search",
            {"root": str(other), "query": "x", "limit": 5},
            key="denied",
        )
    )
    assert allowed.status == ResultStatus.COMPLETED
    assert denied.status == ResultStatus.DENIED


@pytest.mark.asyncio
async def test_only_skills_marked_safe_are_retried(tmp_path):
    safe = FlakySkill(True)
    engine, _, audit = engine_for(tmp_path, safe)
    recovered = await engine.execute(
        make_call("test.flaky", {"target": "x"}, key="safe")
    )
    assert recovered.status == ResultStatus.COMPLETED
    assert safe.count == 2
    assert audit.query(event_type="skill_execution")[0]["details"]["attempts"] == 2

    unsafe = FlakySkill(False)
    unsafe_path = tmp_path / "unsafe"
    unsafe_path.mkdir()
    engine, _, _ = engine_for(unsafe_path, unsafe)
    failed = await engine.execute(
        make_call("test.flaky", {"target": "x"}, key="unsafe")
    )
    assert failed.status == ResultStatus.FAILED
    assert unsafe.count == 1


@pytest.mark.asyncio
async def test_whole_plan_timeout_is_normalized(tmp_path):
    slow = SlowSkill()
    engine, _, _ = engine_for(tmp_path, slow)
    plan = ExecutionPlan(
        steps=(
            PlanStep(
                step_id="slow",
                call=make_call("test.slow", {"target": "x"}, key="slow"),
                timeout_seconds=2,
            ),
        ),
        timeout_seconds=0.05,
    )
    results = await engine.execute_plan(plan)
    assert results["slow"].status == ResultStatus.TIMED_OUT
    assert results["slow"].error.code == "plan_timeout"
