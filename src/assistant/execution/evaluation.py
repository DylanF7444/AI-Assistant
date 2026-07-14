from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError

from assistant.contracts import PermissionScope, RiskLevel, SkillCall, SkillError
from assistant.policy.permissions import PermissionDecision, PermissionManager
from assistant.skills.base import ApplicationRegistrationRequired, Skill
from assistant.skills.registry import SkillRegistry


@dataclass(frozen=True)
class PermissionGrantRequest:
    call: SkillCall
    scope: PermissionScope


@dataclass(frozen=True)
class ApplicationRegistrationRequest:
    call: SkillCall
    application: str


@dataclass(frozen=True)
class CallEvaluation:
    call: SkillCall
    skill: Skill | None = None
    required_permissions: tuple[PermissionScope, ...] = ()
    permission_checks: tuple[tuple[SkillCall, tuple[PermissionScope, ...]], ...] = ()
    decisions: tuple[PermissionDecision, ...] = ()
    permission_requests: tuple[PermissionGrantRequest, ...] = ()
    registration_requests: tuple[ApplicationRegistrationRequest, ...] = ()
    policy_denied: tuple[PermissionScope, ...] = ()
    risk: RiskLevel | None = None
    preview: str | None = None
    invalid_error: SkillError | None = None

    @property
    def allowed(self) -> bool:
        return (
            self.invalid_error is None
            and not self.registration_requests
            and not self.policy_denied
            and not self.permission_requests
            and all(decision.allowed for decision in self.decisions)
        )

    @property
    def needs_confirmation(self) -> bool:
        return self.allowed and self.preview is not None and self.risk is not None


class CallEvaluator:
    def __init__(
        self,
        registry: SkillRegistry,
        permissions: PermissionManager,
        requires_confirmation: Callable[[SkillCall, RiskLevel], bool],
    ) -> None:
        self.registry = registry
        self.permissions = permissions
        self.requires_confirmation = requires_confirmation

    async def evaluate(
        self,
        call: SkillCall,
        *,
        consume_once: bool,
        include_preview: bool,
    ) -> CallEvaluation:
        try:
            skill = self.registry.get(call.skill_id)
            skill.validate(call.arguments)
        except (KeyError, ValidationError, ValueError) as exc:
            return CallEvaluation(
                call=call,
                invalid_error=SkillError(code="invalid_call", message=str(exc)),
            )

        try:
            required_permissions = tuple(skill.required_permissions(call))
            checks = tuple(
                (check_call, tuple(scopes)) for check_call, scopes in skill.permission_checks(call)
            )
        except ApplicationRegistrationRequired as exc:
            return CallEvaluation(
                call=call,
                skill=skill,
                registration_requests=(
                    ApplicationRegistrationRequest(call=call, application=exc.application),
                ),
            )
        except (KeyError, ValidationError, ValueError) as exc:
            return CallEvaluation(
                call=call,
                skill=skill,
                invalid_error=SkillError(code="invalid_call", message=str(exc)),
            )

        decisions = tuple(
            self.permissions.authorize(
                check_call,
                scopes,
                consume_once=consume_once,
            )
            for check_call, scopes in checks
        )
        policy_denied = tuple(scope for decision in decisions for scope in decision.policy_denied)
        permission_requests = tuple(
            PermissionGrantRequest(call=check_call, scope=scope)
            for (check_call, _), decision in zip(checks, decisions, strict=True)
            for scope in decision.missing
        )
        if (
            policy_denied
            or permission_requests
            or any(not decision.allowed for decision in decisions)
        ):
            return CallEvaluation(
                call=call,
                skill=skill,
                required_permissions=required_permissions,
                permission_checks=checks,
                decisions=decisions,
                permission_requests=permission_requests,
                policy_denied=policy_denied,
            )

        try:
            risk = skill.risk_for(call)
            preview = None
            if include_preview and self.requires_confirmation(call, risk):
                preview = await skill.preview(call)
        except (KeyError, ValidationError, ValueError) as exc:
            return CallEvaluation(
                call=call,
                skill=skill,
                invalid_error=SkillError(code="invalid_call", message=str(exc)),
            )
        return CallEvaluation(
            call=call,
            skill=skill,
            required_permissions=required_permissions,
            permission_checks=checks,
            decisions=decisions,
            risk=risk,
            preview=preview,
        )
