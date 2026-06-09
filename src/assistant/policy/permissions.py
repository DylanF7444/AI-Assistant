from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select

from assistant.contracts import PermissionScope, SkillCall
from assistant.storage.database import AuditStore, Database, PermissionRow


class GrantType(StrEnum):
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    missing: tuple[PermissionScope, ...] = ()
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
    def __init__(self, database: Database, audit: AuditStore) -> None:
        self.database = database
        self.audit = audit

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

    def revoke(
        self, user_id: str, skill_id: str, capability: str, correlation_id: str
    ) -> None:
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
                    select(PermissionRow).order_by(
                        PermissionRow.skill_id, PermissionRow.capability
                    )
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

    def authorize(
        self,
        call: SkillCall,
        required: tuple[PermissionScope, ...],
        *,
        consume_once: bool = True,
    ) -> PermissionDecision:
        missing: list[PermissionScope] = []
        now = datetime.now(UTC)
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
                    if not scope.targets or not targets or (
                        all(
                            any(_target_in_scope(target, grant) for grant in targets)
                            for target in scope.targets
                        )
                    ):
                        allowed = True
                        if consume_once and row.grant_type == GrantType.ONCE.value:
                            session.delete(row)
                        break
                if not allowed:
                    missing.append(scope)
        decision = PermissionDecision(
            allowed=not missing,
            missing=tuple(missing),
            reason="" if not missing else "Required permission has not been granted",
        )
        self.audit.record(
            correlation_id=str(call.correlation_id),
            event_type="authorization",
            skill_id=call.skill_id,
            decision="allowed" if decision.allowed else "denied",
            permissions=[scope.capability for scope in required],
            details={"missing": [scope.model_dump() for scope in missing]},
        )
        return decision


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
