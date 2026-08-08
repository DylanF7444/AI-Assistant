from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import ValidationError
from pydantic import BaseModel, ConfigDict, Field

from assistant.context.snapshot import ContextSnapshot
from assistant.contracts import Intent
from assistant.language.understanding import UserIntentInterpretation


class InvalidPlannerResponse(ValidationError):
    def __init__(self, message: str, invalid_response: dict[str, Any]) -> None:
        super().__init__(message)
        self.invalid_response = invalid_response


@dataclass(frozen=True)
class PlannedIntentStep:
    step_id: str
    intent: Intent
    depends_on: tuple[str, ...] = ()
    timeout_seconds: float = 30.0


class PlanRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disposition: Literal["stop", "next_step", "clarify", "reject"]
    rationale: str
    skill_id: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    clarification: str | None = None
    response: str | None = None
    confidence: float = Field(ge=0, le=1)


@dataclass(frozen=True)
class PlannerDecision:
    intent: Intent | None = None
    steps: tuple[PlannedIntentStep, ...] = ()
    clarification: str | None = None
    interpretation: UserIntentInterpretation | None = None
    context: ContextSnapshot | None = None
    retrieved: tuple[Any, ...] = ()
    allow_replanning: bool = False

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarification)
