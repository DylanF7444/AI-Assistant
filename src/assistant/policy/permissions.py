from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select

from assistant.contracts import PermissionContext, PermissionScope, SkillCall
from assistant.policy.file_access import FileAccessPolicyStore
from assistant.storage.database import (
    AuditStore,
    BackgroundAuthorizationRow,
    Database,
    PermissionRow,
    ScheduleRow,
)
from assistant.windows.identity import current_machine_identity


class GrantType(StrEnum):
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    missing: tuple[PermissionScope, ...] = ()
    policy_denied: tuple[PermissionScope, ...] = ()
    reason: str = ""


def _target_in_scope(requested: str, allowed: str) -> bool:
    if allowed == "*":
        return True
    if requested == allowed:
        return True
    try:
        requested_path = Path(requested).resolve()
        allowed_path = Path(allowed).resolve()
        return requested_path == allowed_path or allowed_path in requested_path.parents
    except (OSError, ValueError):
        return False


class PermissionManager:
    def __init__(
        self,
        database: Database,
        audit: AuditStore,
        file_policy_store: FileAccessPolicyStore | None = None,
    ) -> None:
        self.database = database
        self.audit = audit
        self.file_policy_store = file_policy_store

    def grant(
        self,
        *,
        user_id: str,
        skill_id: str,
        capability: str,
        grant_type: GrantType,
        targets: tuple[str, ...] = (),
        session_id: str | None = None,
        expires_at: datetime | None = None,
        correlation_id: str = "permission-change",
    ) -> None:
        if grant_type == GrantType.SESSION and not session_id:
            raise ValueError("Session grants require a session_id")
        with self.database.session() as session:
            statement = select(PermissionRow).where(
                PermissionRow.user_id == user_id,
                PermissionRow.skill_id == skill_id,
                PermissionRow.capability == capability,
                PermissionRow.grant_type == grant_type.value,
                PermissionRow.session_id == session_id,
            )
            row = session.scalar(statement)
            values = {
                "effect": "deny" if grant_type == GrantType.DENY else "allow",
                "targets_json": json.dumps(targets),
                "expires_at": expires_at,
                "created_at": datetime.now(UTC),
            }
            if row is None:
                session.add(
                    PermissionRow(
                        user_id=user_id,
                        skill_id=skill_id,
                        capability=capability,
                        grant_type=grant_type.value,
                        session_id=session_id,
                        **values,
                    )
                )
            else:
                for key, value in values.items():
                    setattr(row, key, value)
        self.audit.record(
            correlation_id=correlation_id,
            event_type="permission_changed",
            skill_id=skill_id,
            decision=grant_type.value,
            permissions=[capability],
            details={"targets": targets, "user_id": user_id},
        )

    def revoke(self, user_id: str, skill_id: str, capability: str, correlation_id: str) -> None:
        with self.database.session() as session:
            session.query(PermissionRow).filter(
                PermissionRow.user_id == user_id,
                PermissionRow.skill_id == skill_id,
                PermissionRow.capability == capability,
            ).delete()
        self.audit.record(
            correlation_id=correlation_id,
            event_type="permission_changed",
            skill_id=skill_id,
            decision="revoked",
            permissions=[capability],
        )

    def list_grants(self) -> list[dict]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(PermissionRow).order_by(PermissionRow.skill_id, PermissionRow.capability)
                )
            )
            return [
                {
                    "user_id": row.user_id,
                    "skill_id": row.skill_id,
                    "capability": row.capability,
                    "effect": row.effect,
                    "grant_type": row.grant_type,
                    "targets": json.loads(row.targets_json),
                    "session_id": row.session_id,
                    "expires_at": row.expires_at,
                }
                for row in rows
            ]

    def allowed_targets(
        self,
        *,
        user_id: str,
        capability: str,
        session_id: str | None = None,
    ) -> tuple[str, ...]:
        now = datetime.now(UTC)
        policy = self.file_policy_store.get() if self.file_policy_store else None
        targets: list[str] = []
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(PermissionRow).where(
                        PermissionRow.user_id == user_id,
                        PermissionRow.capability == capability,
                        PermissionRow.effect == "allow",
                    )
                )
            )
        for row in rows:
            if row.expires_at is not None and _aware(row.expires_at) <= now:
                continue
            if row.grant_type == GrantType.SESSION.value and row.session_id != session_id:
                continue
            for target in json.loads(row.targets_json):
                if target in {"", "*"}:
                    continue
                if policy and policy.denial_reason(capability, target):
                    continue
                normalized = str(Path(target).expanduser().resolve())
                if normalized not in targets:
                    targets.append(normalized)
        return tuple(targets)

    def authorize(
        self,
        call: SkillCall,
        required: tuple[PermissionScope, ...],
        *,
        consume_once: bool = True,
    ) -> PermissionDecision:
        if call.permission_context.invocation == "background":
            return self._authorize_background(call, required)
        missing: list[PermissionScope] = []
        policy_denied: list[PermissionScope] = []
        now = datetime.now(UTC)
        policy = self.file_policy_store.get() if self.file_policy_store else None
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(PermissionRow).where(
                        PermissionRow.user_id == call.permission_context.user_id,
                        PermissionRow.skill_id == call.skill_id,
                    )
                )
            )
            for scope in required:
                policy_reason = None
                if policy and scope.capability in {
                    "filesystem.read",
                    "filesystem.write",
                    "filesystem.content.read",
                    "filesystem.content.index",
                }:
                    for target in scope.targets:
                        policy_reason = policy.denial_reason(scope.capability, target)
                        if policy_reason:
                            break
                if policy_reason:
                    denied_scope = scope.model_copy(update={"reason": policy_reason})
                    policy_denied.append(denied_scope)
                    continue
                applicable = [
                    row
                    for row in rows
                    if row.capability == scope.capability
                    and (row.expires_at is None or _aware(row.expires_at) > now)
                    and (
                        row.grant_type != GrantType.SESSION.value
                        or row.session_id == call.permission_context.session_id
                    )
                ]
                if any(row.effect == "deny" for row in applicable):
                    missing.append(scope)
                    continue
                allowed = False
                for row in applicable:
                    targets = tuple(json.loads(row.targets_json))
                    if (
                        not scope.targets
                        or not targets
                        or (
                            all(
                                any(_target_in_scope(target, grant) for grant in targets)
                                for target in scope.targets
                            )
                        )
                    ):
                        allowed = True
                        if consume_once and row.grant_type == GrantType.ONCE.value:
                            session.delete(row)
                        break
                if not allowed:
                    missing.append(scope)
        reason = ""
        if policy_denied:
            reason = policy_denied[0].reason
        elif missing:
            reason = "Required permission has not been granted"
        decision = PermissionDecision(
            allowed=not missing and not policy_denied,
            missing=tuple(missing),
            policy_denied=tuple(policy_denied),
            reason=reason,
        )
        self.audit.record(
            correlation_id=str(call.correlation_id),
            event_type="authorization",
            skill_id=call.skill_id,
            decision="allowed" if decision.allowed else "denied",
            permissions=[scope.capability for scope in required],
            details={
                "missing": [scope.model_dump() for scope in missing],
                "policy_denied": [scope.model_dump() for scope in policy_denied],
            },
        )
        return decision

    def authorize_scopes(
        self,
        *,
        user_id: str,
        subject_id: str,
        session_id: str,
        required: tuple[PermissionScope, ...],
        consume_once: bool = True,
    ) -> PermissionDecision:
        """Authorize a typed, non-executing principal through the normal grant store."""
        call = SkillCall(
            skill_id=subject_id,
            caller="user",
            permission_context=PermissionContext(
                user_id=user_id,
                session_id=session_id,
            ),
        )
        return self.authorize(call, required, consume_once=consume_once)

    def _authorize_background(
        self,
        call: SkillCall,
        required: tuple[PermissionScope, ...],
    ) -> PermissionDecision:
        context = call.permission_context
        assert context.schedule_id is not None
        assert context.schedule_revision is not None
        now = datetime.now(UTC)
        policy = self.file_policy_store.get() if self.file_policy_store else None
        with self.database.session() as session:
            authorization = session.scalar(
                select(BackgroundAuthorizationRow).where(
                    BackgroundAuthorizationRow.schedule_id == str(context.schedule_id),
                    BackgroundAuthorizationRow.schedule_revision == context.schedule_revision,
                    BackgroundAuthorizationRow.user_id == context.user_id,
                    BackgroundAuthorizationRow.revoked_at.is_(None),
                )
            )
            schedule = session.get(ScheduleRow, str(context.schedule_id))
        missing: list[PermissionScope] = []
        policy_denied: list[PermissionScope] = []
        reason = ""
        if authorization is None or (
            authorization.expires_at is not None and _aware(authorization.expires_at) <= now
        ):
            missing.extend(required)
            reason = "Matching background authorization is missing or expired"
            allowed = False
        elif (
            schedule is None
            or authorization.machine_identity != current_machine_identity()
            or schedule.revision != context.schedule_revision
            or schedule.status not in {"active", "completed"}
            or schedule.action_digest != authorization.action_digest
            or hashlib.sha256(schedule.autonomy_policy_json.encode("utf-8")).hexdigest()
            != authorization.policy_digest
            or not self._background_call_matches(schedule.action_json, call)
        ):
            missing.extend(required)
            reason = "Call does not match the authorized schedule action"
            allowed = False
        else:
            skill_ids = set(json.loads(authorization.allowed_skill_ids_json))
            capabilities = set(json.loads(authorization.allowed_capabilities_json))
            targets = tuple(json.loads(authorization.allowed_targets_json))
            if call.skill_id not in skill_ids:
                missing.extend(required)
                reason = "Skill is outside the background authorization"
            else:
                for scope in required:
                    policy_reason = None
                    if policy and scope.capability in {
                        "filesystem.read",
                        "filesystem.write",
                        "filesystem.content.read",
                        "filesystem.content.index",
                    }:
                        for target in scope.targets:
                            policy_reason = policy.denial_reason(scope.capability, target)
                            if policy_reason:
                                break
                    if policy_reason:
                        policy_denied.append(scope.model_copy(update={"reason": policy_reason}))
                        continue
                    target_allowed = not scope.targets or (
                        bool(targets)
                        and all(
                            any(_target_in_scope(requested, granted) for granted in targets)
                            for requested in scope.targets
                        )
                    )
                    if scope.capability not in capabilities or not target_allowed:
                        missing.append(scope)
            allowed = not missing and not policy_denied and call.skill_id in skill_ids
            if not reason and policy_denied:
                reason = policy_denied[0].reason
            elif not reason and missing:
                reason = "Action is outside the background authorization scope"
        decision = PermissionDecision(
            allowed=allowed,
            missing=tuple(missing),
            policy_denied=tuple(policy_denied),
            reason=reason,
        )
        self.audit.record(
            correlation_id=str(call.correlation_id),
            event_type="authorization",
            skill_id=call.skill_id,
            decision="allowed" if decision.allowed else "denied",
            permissions=[scope.capability for scope in required],
            details={
                "background": True,
                "schedule_id": str(context.schedule_id),
                "schedule_revision": context.schedule_revision,
                "missing": [scope.model_dump() for scope in missing],
                "policy_denied": [scope.model_dump() for scope in policy_denied],
            },
        )
        return decision

    @staticmethod
    def _background_call_matches(action_json: str, call: SkillCall) -> bool:
        action = json.loads(action_json)
        if action.get("kind") == "fixed_skill":
            templates = (action.get("call", {}),)
        elif action.get("kind") == "frozen_plan":
            templates = tuple(
                step.get("call", {}) for step in action.get("plan", {}).get("steps", ())
            )
        elif action.get("kind") == "logic_workflow":
            workflow = action.get("workflow", {})
            templates_list = []
            if isinstance(workflow.get("check"), dict):
                templates_list.append(workflow["check"])
            common_action = workflow.get("action", {})
            if isinstance(common_action, dict) and common_action.get("kind") == "skill":
                templates_list.append(common_action.get("call", {}))
            selection = workflow.get("selection", {})
            if isinstance(selection, dict):
                templates_list.extend(
                    option.get("action", {})
                    for option in selection.get("options", ())
                    if isinstance(option, dict) and isinstance(option.get("action"), dict)
                )
            templates = tuple(templates_list)
        else:
            return False
        return any(
            template.get("skill_id") == call.skill_id
            and template.get("arguments", {}) == call.arguments
            for template in templates
        )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
