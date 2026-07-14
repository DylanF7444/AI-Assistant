from __future__ import annotations

import pytest
from pydantic import BaseModel

from assistant.contracts import (
    PermissionContext,
    PermissionScope,
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
from assistant.skills.builtin import CalculatorSkill, manifest
from assistant.skills.macros import MacroSkill
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import AuditStore, Database
from assistant.storage.macros import MacroDefinition, MacroStep, MacroStore


class ActionInput(BaseModel):
    value: str


class ActionOutput(BaseModel):
    value: str


class ActionSkill(Skill):
    input_model = ActionInput
    output_model = ActionOutput
    manifest = manifest(
        skill_id="test.action",
        display_name="Action",
        description="A permission-gated test action.",
        input_model=input_model,
        output_model=output_model,
        permissions=(PermissionScope(capability="test.action", reason="Run test action"),),
        risk=RiskLevel.HIGH,
    )

    async def execute(self, call, cancel_event):
        value = self.validate(call.arguments).value
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Action complete.",
            data={"value": value},
        )


def setup(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    audit = AuditStore(database)
    permissions = PermissionManager(database, audit)
    registry = SkillRegistry(require_signatures=False)
    registry.register(CalculatorSkill())
    registry.register(ActionSkill())
    store = MacroStore(database)
    macro = MacroSkill(store, registry, permissions, audit)
    registry.register(macro)
    engine = ExecutionEngine(registry, permissions, ConfirmationManager(), audit, EventBus())
    return store, engine, permissions, audit


@pytest.mark.asyncio
async def test_safe_macro_runs_ordered_validated_steps(tmp_path):
    store, engine, _, audit = setup(tmp_path)
    store.save(
        MacroDefinition(
            name="math",
            description="Two calculations",
            steps=(
                MacroStep("core.calculator", {"expression": "2 + 3"}),
                MacroStep("core.calculator", {"expression": "4 * 5"}),
            ),
        )
    )
    call = SkillCall(
        skill_id="core.macro",
        arguments={"name": "math"},
        permission_context=PermissionContext(session_id="test"),
    )
    outcome = await engine.prepare(call)
    assert outcome.result.status == ResultStatus.COMPLETED
    assert [item["data"]["value"] for item in outcome.result.data["results"]] == [
        5.0,
        20.0,
    ]
    assert len(audit.query(event_type="macro_step")) == 2


@pytest.mark.asyncio
async def test_sensitive_macro_requires_permission_and_full_plan_confirmation(tmp_path):
    store, engine, permissions, _ = setup(tmp_path)
    store.save(
        MacroDefinition(
            name="sensitive",
            description="Sensitive action",
            steps=(MacroStep("test.action", {"value": "exact"}),),
        )
    )
    call = SkillCall(
        skill_id="core.macro",
        arguments={"name": "sensitive"},
        permission_context=PermissionContext(session_id="test"),
    )
    denied = await engine.prepare(call)
    assert denied.result.status == ResultStatus.DENIED
    permissions.grant(
        user_id="local-user",
        skill_id="test.action",
        capability="test.action",
        grant_type=GrantType.ALWAYS,
    )
    prepared = await engine.prepare(call)
    assert prepared.confirmation.risk == RiskLevel.HIGH
    assert "Action" in prepared.confirmation.exact_action
    result = await engine.execute(call, confirmation_id=prepared.confirmation.confirmation_id)
    assert result.status == ResultStatus.COMPLETED


@pytest.mark.asyncio
async def test_unknown_macro_name_is_an_invalid_call_instead_of_preparation_exception(tmp_path):
    _, engine, _, _ = setup(tmp_path)
    call = SkillCall(
        skill_id="core.macro",
        arguments={"name": "SeparateFilesIntoFolders"},
        permission_context=PermissionContext(session_id="test"),
    )

    outcome = await engine.prepare(call)

    assert outcome.result.status == ResultStatus.DENIED
    assert outcome.result.error.code == "invalid_call"
    assert "Unknown configured macro" in outcome.result.error.message


def test_macro_rejects_recursion_and_invalid_skill(tmp_path):
    store, engine, _, _ = setup(tmp_path)
    store.save(
        MacroDefinition(
            name="recursive",
            description="bad",
            steps=(MacroStep("core.macro", {"name": "recursive"}),),
        )
    )
    macro = engine.registry.get("core.macro")
    call = SkillCall(
        skill_id="core.macro",
        arguments={"name": "recursive"},
        permission_context=PermissionContext(session_id="test"),
    )
    with pytest.raises(ValueError, match="cannot call"):
        macro.required_permissions(call)
