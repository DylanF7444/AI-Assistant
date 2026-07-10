import pytest

from assistant.contracts import PermissionContext, ResultStatus, SkillCall
from assistant.events import EventBus
from assistant.execution.engine import ExecutionEngine
from assistant.policy.confirmations import ConfirmationManager
from assistant.policy.permissions import PermissionManager
from assistant.skills.builtin import CalculatorSkill
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import AuditStore, Database


def build_engine(database: Database, audit: AuditStore) -> ExecutionEngine:
    registry = SkillRegistry()
    registry.register(CalculatorSkill())
    return ExecutionEngine(
        registry=registry,
        permissions=PermissionManager(database, audit),
        confirmations=ConfirmationManager(),
        audit=audit,
        events=EventBus(),
    )


def calculator_call(key: str = "same-request") -> SkillCall:
    return SkillCall(
        skill_id="core.calculator",
        arguments={"expression": "6 * 7"},
        permission_context=PermissionContext(session_id="learning-session"),
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_execution_engine_runs_validates_audits_and_emits_events(
    database: Database,
    audit: AuditStore,
):
    engine = build_engine(database, audit)
    call = calculator_call()

    result = await engine.execute(call)

    assert result.status == ResultStatus.COMPLETED
    assert result.data["value"] == 42
    assert engine.events.history(call.correlation_id)[0].state.value == "executing"
    assert audit.query(event_type="skill_execution")[0]["decision"] == "completed"


@pytest.mark.asyncio
async def test_idempotency_key_returns_the_previous_completed_result(
    database: Database,
    audit: AuditStore,
):
    engine = build_engine(database, audit)
    first_call = calculator_call()
    changed_call = first_call.model_copy(update={"arguments": {"expression": "100 + 1"}})

    first = await engine.execute(first_call)
    duplicate = await engine.execute(changed_call)

    assert first.data["value"] == 42
    assert duplicate == first
    assert len(audit.query(event_type="skill_execution")) == 1
