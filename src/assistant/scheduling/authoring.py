from __future__ import annotations

from uuid import UUID, uuid4

from assistant.contracts import (
    ExecutionPlan,
    PermissionContext,
    PlanStep,
    RiskLevel,
    SkillCall,
)
from assistant.policy.background import background_compatibility
from assistant.scheduling.contracts import (
    AutonomyPolicy,
    DeliveryPolicy,
    FixedSkillAction,
    FrozenPlanAction,
    ScheduleDefinition,
    ScheduledTrigger,
    ScheduleStatus,
    SkillSnapshot,
)
from assistant.scheduling.store import ScheduleStore, digest
from assistant.skills.registry import SkillRegistry
from assistant.storage.macros import MacroStore

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}

_FORBIDDEN_BACKGROUND_SKILLS = {
    "core.file_access_policy",
    "core.macro",
    "core.schedule",
}


class ScheduleAuthoringService:
    def __init__(
        self,
        store: ScheduleStore,
        registry: SkillRegistry,
        macros: MacroStore,
    ) -> None:
        self.store = store
        self.registry = registry
        self.macros = macros

    def create_fixed_action(
        self,
        *,
        name: str,
        trigger: ScheduledTrigger,
        skill_id: str,
        arguments: dict,
        user_id: str = "local-user",
        description: str = "",
        delivery_policy: DeliveryPolicy | None = None,
    ) -> ScheduleDefinition:
        schedule_id = uuid4()
        call, snapshot, capabilities, targets, risk = self._prepared_call(
            schedule_id,
            1,
            skill_id,
            arguments,
            step_id="action",
            user_id=user_id,
        )
        now = self.store.clock.now()
        definition = ScheduleDefinition(
            schedule_id=schedule_id,
            user_id=user_id,
            name=name,
            description=description,
            status=ScheduleStatus.PENDING_AUTHORIZATION,
            trigger=trigger,
            action=FixedSkillAction(call=call),
            autonomy_policy=self._policy((skill_id,), capabilities, targets, risk, max_steps=1),
            delivery_policy=delivery_policy or DeliveryPolicy(),
            skill_snapshot=(snapshot,),
            created_at=now,
            updated_at=now,
        )
        return self.store.create(definition, authorize=True)

    def create_macro(
        self,
        *,
        name: str,
        trigger: ScheduledTrigger,
        macro_name: str,
        user_id: str = "local-user",
        description: str = "",
        delivery_policy: DeliveryPolicy | None = None,
    ) -> ScheduleDefinition:
        macro = self.macros.get(macro_name)
        schedule_id = uuid4()
        calls = []
        snapshots = []
        capabilities: set[str] = set()
        targets: set[str] = set()
        risks = []
        previous = None
        for index, step in enumerate(macro.steps):
            step_id = f"step-{index + 1}"
            call, snapshot, call_capabilities, call_targets, risk = self._prepared_call(
                schedule_id,
                1,
                step.skill_id,
                step.arguments,
                step_id=step_id,
                user_id=user_id,
            )
            calls.append(
                PlanStep(
                    step_id=step_id,
                    call=call,
                    depends_on=(previous,) if previous else (),
                )
            )
            snapshots.append(snapshot)
            capabilities.update(call_capabilities)
            targets.update(call_targets)
            risks.append(risk)
            previous = step_id
        if not calls:
            raise ValueError("Macro must contain at least one step")
        effective_risk = max(risks, key=_RISK_ORDER.get)
        now = self.store.clock.now()
        definition = ScheduleDefinition(
            schedule_id=schedule_id,
            user_id=user_id,
            name=name,
            description=description or f"Frozen snapshot of macro {macro.name}",
            status=ScheduleStatus.PENDING_AUTHORIZATION,
            trigger=trigger,
            action=FrozenPlanAction(plan=ExecutionPlan(steps=tuple(calls))),
            autonomy_policy=self._policy(
                tuple(call.call.skill_id for call in calls),
                capabilities,
                targets,
                effective_risk,
                max_steps=len(calls),
            ),
            delivery_policy=delivery_policy or DeliveryPolicy(),
            skill_snapshot=tuple(snapshots),
            created_at=now,
            updated_at=now,
        )
        return self.store.create(definition, authorize=True)

    def risk_for_action(self, skill_id: str, arguments: dict) -> RiskLevel:
        self._ensure_allowed(skill_id)
        skill = self.registry.get(skill_id)
        call = SkillCall(
            skill_id=skill_id,
            caller="scheduler",
            arguments=arguments,
            permission_context=PermissionContext(
                session_id="schedule-preview",
                invocation="background",
                schedule_id=uuid4(),
                schedule_revision=1,
            ),
        )
        skill.validate(arguments)
        return skill.risk_for(call)

    def risk_for_macro(self, macro_name: str) -> RiskLevel:
        risks = [
            self.risk_for_action(step.skill_id, step.arguments)
            for step in self.macros.get(macro_name).steps
        ]
        return max(risks, key=_RISK_ORDER.get, default=RiskLevel.LOW)

    async def preview_action(self, skill_id: str, arguments: dict) -> str:
        self._ensure_allowed(skill_id)
        skill = self.registry.get(skill_id)
        call = SkillCall(
            skill_id=skill_id,
            caller="scheduler",
            arguments=arguments,
            permission_context=PermissionContext(
                session_id="schedule-preview",
                invocation="background",
                schedule_id=uuid4(),
                schedule_revision=1,
            ),
        )
        return await skill.preview(call)

    def _prepared_call(
        self,
        schedule_id: UUID,
        revision: int,
        skill_id: str,
        arguments: dict,
        *,
        step_id: str,
        user_id: str,
    ):
        self._ensure_allowed(skill_id)
        self._ensure_no_embedded_secrets(arguments)
        skill = self.registry.get(skill_id)
        call = SkillCall(
            skill_id=skill_id,
            caller="scheduler",
            arguments=arguments,
            permission_context=PermissionContext(
                user_id=user_id,
                session_id=f"scheduler:{schedule_id}",
                invocation="background",
                schedule_id=schedule_id,
                schedule_revision=revision,
            ),
            idempotency_key=f"schedule-template:{schedule_id}:{revision}:{step_id}",
        )
        skill.validate(arguments)
        permissions = tuple(skill.required_permissions(call))
        compatibility = background_compatibility(skill.manifest, permissions)
        if not compatibility.allowed:
            raise ValueError(
                "Skill cannot run without interrupting the interactive desktop: "
                f"{skill_id}. {compatibility.reason}"
            )
        risk = skill.risk_for(call)
        snapshot = SkillSnapshot(
            skill_id=skill_id,
            version=skill.manifest.version,
            risk=risk,
            permissions_digest=digest(
                [permission.model_dump(mode="json") for permission in permissions]
            ),
            input_schema_digest=digest(skill.input_schema()),
        )
        capabilities = {permission.capability for permission in permissions}
        targets = {target for permission in permissions for target in permission.targets}
        return call, snapshot, capabilities, targets, risk

    @staticmethod
    def _policy(
        skill_ids,
        capabilities,
        targets,
        risk: RiskLevel,
        *,
        max_steps: int,
    ) -> AutonomyPolicy:
        return AutonomyPolicy(
            allowed_skill_ids=tuple(sorted(set(skill_ids))),
            allowed_capabilities=tuple(sorted(set(capabilities))),
            allowed_targets=tuple(sorted(set(targets))),
            max_risk=risk,
            max_steps=max_steps,
            max_runtime_seconds=min(600, max(30, max_steps * 30)),
            allow_network=any(
                capability == "network" or capability.startswith("network.")
                for capability in capabilities
            ),
            requires_unlocked_session=False,
        )

    @staticmethod
    def _ensure_allowed(skill_id: str) -> None:
        if skill_id in _FORBIDDEN_BACKGROUND_SKILLS:
            raise ValueError(f"Skill cannot be scheduled for background execution: {skill_id}")

    @classmethod
    def _ensure_no_embedded_secrets(cls, value, path: str = "arguments") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.casefold() in {"password", "token", "secret", "api_key", "pin"}:
                    raise ValueError(
                        f"Scheduled actions cannot persist secret field {path}.{key}; "
                        "use a configured secret reference"
                    )
                cls._ensure_no_embedded_secrets(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                cls._ensure_no_embedded_secrets(item, f"{path}[{index}]")
