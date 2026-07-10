from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from assistant.contracts import ConfirmationRequest, RiskLevel, SkillCall


def action_digest(call: SkillCall) -> str:
    canonical = json.dumps(
        {"skill_id": call.skill_id, "arguments": call.arguments},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConfirmationManager:
    def __init__(self) -> None:
        self._pending: dict[UUID, ConfirmationRequest] = {}

    def create(
        self,
        call: SkillCall,
        *,
        exact_action: str,
        targets: tuple[str, ...],
        risk: RiskLevel,
        ttl_seconds: int = 30,
    ) -> ConfirmationRequest:
        request = ConfirmationRequest(
            correlation_id=call.correlation_id,
            action_digest=action_digest(call),
            skill_id=call.skill_id,
            exact_action=exact_action,
            exact_targets=targets,
            risk=risk,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            requires_typed_token=risk == RiskLevel.CRITICAL,
        )
        self._pending[request.confirmation_id] = request
        return request

    def approve(
        self,
        confirmation_id: UUID,
        call: SkillCall,
        *,
        typed_token: str | None = None,
    ) -> bool:
        request = self._pending.pop(confirmation_id, None)
        if request is None or request.expires_at <= datetime.now(UTC):
            return False
        if request.correlation_id != call.correlation_id:
            return False
        if not hmac.compare_digest(request.action_digest, action_digest(call)):
            return False
        return not (request.requires_typed_token and typed_token != str(confirmation_id)[-6:])

    def deny(self, confirmation_id: UUID) -> bool:
        return self._pending.pop(confirmation_id, None) is not None

    def cancel_all(self) -> None:
        self._pending.clear()
