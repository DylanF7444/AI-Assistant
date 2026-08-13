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
from assistant.integrations.openai import OpenAIAdapter
from assistant.integrations.web import CodexExecClient
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest

OPENAI_NETWORK_PERMISSION = (
    PermissionScope(
        capability="network",
        targets=("https://api.openai.com",),
        reason="Send the approved prompt to OpenAI's fixed API origin.",
    ),
)
OPENAI_CODEX_PERMISSIONS = (
    PermissionScope(
        capability="network",
        reason="Allow the local Codex CLI to reach its configured service.",
    ),
    PermissionScope(
        capability="process.launch",
        targets=("codex",),
        reason="Start the local Codex CLI in its read-only sandbox.",
    ),
)
OPENAI_MANIFEST_PERMISSIONS = (*OPENAI_NETWORK_PERMISSION, *OPENAI_CODEX_PERMISSIONS)


class OpenAIInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "codex"] = "openai"
    question: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    max_tokens: int = Field(default=1_500, ge=1, le=16_000)

    @field_validator("question")
    @classmethod
    def _question_must_not_contain_unsafe_controls(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("OpenAI question cannot be blank")
        if any(
            ord(character) < 32 and character not in {"\n", "\r", "\t"}
            for character in value
        ):
            raise ValueError("OpenAI request text cannot contain control characters")
        return value

    @field_validator("model")
    @classmethod
    def _model_must_not_contain_controls(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("OpenAI model cannot be blank")
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("OpenAI model cannot contain control characters")
        return value


class OpenAIOutput(BaseModel):
    provider: Literal["openai", "codex"] = "openai"
    model: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=100_000)


class OpenAISkill(Skill):
    input_model = OpenAIInput
    output_model = OpenAIOutput

    def __init__(
        self,
        adapter: OpenAIAdapter,
        default_model: str = "gpt-5.5",
        *,
        codex_client: CodexExecClient | None = None,
        default_codex_model: str = "gpt-5.5",
    ) -> None:
        if not default_model.strip():
            raise ValueError("OpenAI default model cannot be blank")
        if not default_codex_model.strip():
            raise ValueError("OpenAI Codex default model cannot be blank")
        self.adapter = adapter
        self.default_model = default_model.strip()
        self.codex_client = codex_client or CodexExecClient()
        self.default_codex_model = default_codex_model.strip()
        self.manifest = manifest(
            skill_id="integration.openai",
            display_name="OpenAI",
            description=(
                "Ask OpenAI Responses API models with an encrypted API key, or use the local "
                "Codex CLI in its read-only sandbox. Prompt text is omitted from activity "
                "details."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=OPENAI_MANIFEST_PERMISSIONS,
            risk=RiskLevel.LOW,
            safe_to_retry=False,
            background_mode=BackgroundMode.FOREGROUND_ONLY,
        ).model_copy(update={"entry_point": f"{__name__}:OpenAISkill"})

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return OPENAI_CODEX_PERMISSIONS if args.provider == "codex" else OPENAI_NETWORK_PERMISSION

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.provider == "codex":
            model = args.model or self.default_codex_model
            return f"Ask local Codex using {model}; prompt content hidden for privacy."
        model = args.model or self.default_model
        return f"Ask OpenAI using {model}; prompt content hidden for privacy."

    async def health(self) -> dict[str, object]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="OpenAI request cancelled.")
        if args.provider == "codex":
            model = args.model or self.default_codex_model
            answer = await self.codex_client.ask(
                args.question,
                model=model,
                cancel_event=cancel_event,
            )
            summary = f"Codex answered using {model}."
        else:
            model = args.model or self.default_model
            answer = await self.adapter.ask(model, args.question, args.max_tokens)
            summary = f"OpenAI answered using {model}."
        output = OpenAIOutput(provider=args.provider, model=model, answer=answer)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            display_text=answer,
            speech_text=answer,
            data=output.model_dump(mode="json"),
            sensitive=True,
        )


OpenAiSkill = OpenAISkill
