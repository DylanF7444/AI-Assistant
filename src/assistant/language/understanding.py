from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from assistant.contracts import normalize_confidence


class UserIntentInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_goal: str
    request_type: Literal[
        "conversation",
        "question",
        "local_action",
        "multi_step_goal",
        "automation",
        "preference_memory",
        "unsupported",
    ]
    explicit_requests: list[str] = Field(default_factory=list)
    implicit_needs: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    relevant_context_keys: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    should_ask_clarification: bool = False
    clarification_question: str | None = None
    confidence: float = Field(ge=0, le=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value):
        return normalize_confidence(value)
