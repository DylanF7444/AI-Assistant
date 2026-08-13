from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.integrations.anthropic import AnthropicAdapter
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest

ANTHROPIC_NETWORK_PERMISSION = (
    PermissionScope(
        capability="network",
        targets=("https://api.anthropic.com",),
        reason="Send the approved prompt to Anthropic's fixed API origin.",
    ),
)


class AnthropicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    max_tokens: int = Field(default=1_500, ge=1, le=16_000)

    @field_validator("question")
    @classmethod
    def _question_must_not_contain_unsafe_controls(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Anthropic question cannot be blank")
        if any(
            ord(character) < 32 and character not in {"\n", "\r", "\t"}
            for character in value
        ):
            raise ValueError("Anthropic request text cannot contain control characters")
        return value

    @field_validator("model")
    @classmethod
    def _model_must_not_contain_controls(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Anthropic model cannot be blank")
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("Anthropic model cannot contain control characters")
        return value


class AnthropicOutput(BaseModel):
    provider: Literal["anthropic"] = "anthropic"
    model: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=100_000)


class AnthropicSkill(Skill):
    input_model = AnthropicInput
    output_model = AnthropicOutput

    def __init__(
        self,
        adapter: AnthropicAdapter,
        default_model: str = "claude-sonnet-4-6",
    ) -> None:
        if not default_model.strip():
            raise ValueError("Anthropic default model cannot be blank")
        self.adapter = adapter
        self.default_model = default_model.strip()
        self.manifest = manifest(
            skill_id="integration.anthropic",
            display_name="Anthropic",
            description=(
                "Ask Anthropic Claude models through the Messages API using an encrypted API "
                "key. Prompt text is sent only to the fixed Anthropic API origin and is omitted "
                "from activity details."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=ANTHROPIC_NETWORK_PERMISSION,
            risk=RiskLevel.LOW,
            safe_to_retry=False,
            background_mode=BackgroundMode.FOREGROUND_ONLY,
        ).model_copy(update={"entry_point": f"{__name__}:AnthropicSkill"})

    def required_permissions(self, call: SkillCall):
        self.validate(call.arguments)
        return ANTHROPIC_NETWORK_PERMISSION

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        model = args.model or self.default_model
        return f"Ask Anthropic using {model}; prompt content hidden for privacy."

    async def health(self) -> dict[str, object]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(
                status=ResultStatus.CANCELLED,
                summary="Anthropic request cancelled.",
            )
        model = args.model or self.default_model
        answer = await self.adapter.ask(model, args.question, args.max_tokens)
        output = AnthropicOutput(model=model, answer=answer)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Anthropic answered using {model}.",
            display_text=answer,
            speech_text=answer,
            data=output.model_dump(mode="json"),
            sensitive=True,
        )
