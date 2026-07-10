from __future__ import annotations

import asyncio

from pydantic import BaseModel

from assistant.contracts import (
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.policy.permissions import PermissionManager
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import AuditStore
from assistant.storage.macros import MacroStore


class MacroInput(BaseModel):
    name: str


class MacroOutput(BaseModel):
    name: str
    results: list[dict]


_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


class MacroSkill(Skill):
    input_model = MacroInput
    output_model = MacroOutput

    def __init__(
        self,
        store: MacroStore,
        registry: SkillRegistry,
        permissions: PermissionManager,
        audit: AuditStore,
    ) -> None:
        self.store = store
        self.registry = registry
        self.permissions = permissions
        self.audit = audit
        self.manifest = manifest(
            skill_id="core.macro",
            display_name="Macro",
            description="Run a configured sequence of already registered skills.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
        )

    def _calls(self, call: SkillCall) -> tuple[tuple[Skill, SkillCall], ...]:
        definition = self.store.get(self.validate(call.arguments).name)
        values = []
        for index, step in enumerate(definition.steps):
            if step.skill_id == self.manifest.skill_id:
                raise ValueError("Macros cannot call other macros")
            skill = self.registry.get(step.skill_id)
            nested = SkillCall(
                skill_id=step.skill_id,
                caller="macro",
                arguments=step.arguments,
                permission_context=call.permission_context,
                correlation_id=call.correlation_id,
                idempotency_key=f"{call.idempotency_key}:{index}",
            )
            skill.validate(nested.arguments)
            values.append((skill, nested))
        return tuple(values)

    def required_permissions(self, call: SkillCall):
        scopes: list[PermissionScope] = []
        for skill, nested in self._calls(call):
            scopes.extend(skill.required_permissions(nested))
        return tuple(scopes)

    def permission_checks(self, call: SkillCall):
        return tuple(
            (nested, skill.required_permissions(nested)) for skill, nested in self._calls(call)
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        risks = [skill.risk_for(nested) for skill, nested in self._calls(call)]
        return max(risks, key=_RISK_ORDER.get, default=RiskLevel.LOW)

    async def preview(self, call: SkillCall) -> str:
        definition = self.store.get(self.validate(call.arguments).name)
        previews = [await skill.preview(nested) for skill, nested in self._calls(call)]
        return f"Run macro {definition.name}: " + " then ".join(previews)

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        definition = self.store.get(self.validate(call.arguments).name)
        results = []
        for skill, nested in self._calls(call):
            if cancel_event.is_set():
                return SkillResult(
                    status=ResultStatus.CANCELLED,
                    summary=f"Macro {definition.name} was cancelled.",
                    data={"name": definition.name, "results": results},
                )
            decision = self.permissions.authorize(nested, skill.required_permissions(nested))
            if not decision.allowed:
                return SkillResult(
                    status=ResultStatus.PARTIAL if results else ResultStatus.DENIED,
                    summary=f"Macro stopped before {skill.manifest.display_name}: permission denied.",
                    data={"name": definition.name, "results": results},
                )
            try:
                result = await asyncio.wait_for(skill.execute(nested, cancel_event), timeout=30)
                skill.validate_result(result)
            except TimeoutError:
                result = SkillResult(
                    status=ResultStatus.TIMED_OUT,
                    summary=f"{skill.manifest.display_name} timed out.",
                )
            except Exception as exc:
                result = SkillResult(
                    status=ResultStatus.FAILED,
                    summary=f"{skill.manifest.display_name} failed.",
                    data={"error": str(exc)},
                )
            results.append(
                {
                    "skill_id": nested.skill_id,
                    "status": result.status.value,
                    "summary": result.summary,
                    "data": result.data,
                }
            )
            self.audit.record(
                correlation_id=str(call.correlation_id),
                event_type="macro_step",
                skill_id=nested.skill_id,
                decision=result.status.value,
                permissions=[item.capability for item in skill.required_permissions(nested)],
                details={"macro": definition.name, "result": result.model_dump(mode="json")},
            )
            if result.status != ResultStatus.COMPLETED:
                return SkillResult(
                    status=ResultStatus.PARTIAL,
                    summary=f"Macro {definition.name} stopped at {skill.manifest.display_name}.",
                    data={"name": definition.name, "results": results},
                )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Macro {definition.name} completed.",
            data=MacroOutput(name=definition.name, results=results).model_dump(),
        )
