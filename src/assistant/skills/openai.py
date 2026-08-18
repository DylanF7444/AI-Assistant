from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant.config import DEFAULT_CODEX_MODEL
from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.integrations.anthropic import AnthropicAdapter
from assistant.integrations.cloud import CloudPricing
from assistant.integrations.openai import OpenAIAdapter
from assistant.integrations.web import CodexExecClient
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.skills.cloud import (
    CloudAttemptOutput,
    CloudExecutionFailure,
    CloudUsageOutput,
    cloud_failure_result,
    cloud_output_fields,
    cloud_success_summary,
    execute_cloud_request,
)

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
ANTHROPIC_FALLBACK_PERMISSION = (
    PermissionScope(
        capability="network",
        targets=("https://api.anthropic.com",),
        reason="Send the approved prompt to Anthropic only when the user names it as a fallback.",
    ),
)
OPENAI_MANIFEST_PERMISSIONS = (
    *OPENAI_NETWORK_PERMISSION,
    *ANTHROPIC_FALLBACK_PERMISSION,
    *OPENAI_CODEX_PERMISSIONS,
)


class OpenAIInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "codex"] = "openai"
    question: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    max_tokens: int = Field(default=1_500, ge=1, le=16_000)
    fallback_provider: Literal["openai", "anthropic", "codex"] | None = None
    fallback_model: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("question")
    @classmethod
    def _question_must_not_contain_unsafe_controls(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("OpenAI question cannot be blank")
        if any(ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in value):
            raise ValueError("OpenAI request text cannot contain control characters")
        return value

    @field_validator("model", "fallback_model")
    @classmethod
    def _model_must_not_contain_controls(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("OpenAI model cannot be blank")
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("OpenAI model cannot contain control characters")
        return value

    @model_validator(mode="after")
    def _fallback_must_be_explicit_and_distinct(self) -> OpenAIInput:
        if self.fallback_model is not None and self.fallback_provider is None:
            raise ValueError("fallback_model requires fallback_provider")
        if self.fallback_provider == self.provider:
            raise ValueError("Fallback provider must differ from the primary provider")
        return self


class OpenAIOutput(BaseModel):
    provider: Literal["openai", "anthropic", "codex"] = "openai"
    model: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=100_000)
    usage: CloudUsageOutput | None = None
    attempts: tuple[CloudAttemptOutput, ...] = ()
    fallback_used: bool = False


class OpenAISkill(Skill):
    input_model = OpenAIInput
    output_model = OpenAIOutput

    def __init__(
        self,
        adapter: OpenAIAdapter,
        default_model: str = "gpt-5.5",
        *,
        codex_client: CodexExecClient | None = None,
        default_codex_model: str = DEFAULT_CODEX_MODEL,
        anthropic_adapter: AnthropicAdapter | None = None,
        default_anthropic_model: str = "claude-sonnet-4-6",
        pricing: CloudPricing | None = None,
        offline_lock: bool = False,
        provider_availability: Mapping[str, bool] | None = None,
    ) -> None:
        if not default_model.strip():
            raise ValueError("OpenAI default model cannot be blank")
        if not default_codex_model.strip():
            raise ValueError("OpenAI Codex default model cannot be blank")
        if not default_anthropic_model.strip():
            raise ValueError("Anthropic fallback default model cannot be blank")
        self.adapter = adapter
        self.default_model = default_model.strip()
        self.codex_client = codex_client or CodexExecClient()
        self.default_codex_model = default_codex_model.strip()
        self.anthropic_adapter = anthropic_adapter
        self.default_anthropic_model = default_anthropic_model.strip()
        self.pricing = pricing
        self.offline_lock = bool(offline_lock)
        self._provider_availability: dict[str, bool] = {
            "openai": True,
            "anthropic": anthropic_adapter is not None,
            "codex": True,
        }
        if provider_availability is not None:
            self.set_provider_availability(provider_availability)
        if pricing is not None:
            setter = getattr(self.adapter, "set_pricing", None)
            if callable(setter):
                setter(pricing)
            fallback_setter = getattr(self.anthropic_adapter, "set_pricing", None)
            if callable(fallback_setter):
                fallback_setter(pricing)
        self.manifest = manifest(
            skill_id="integration.openai",
            display_name="OpenAI",
            description=(
                "Ask OpenAI Responses API models with an encrypted API key, use local Codex, "
                "or name Anthropic as an explicit fallback. Prompts and answers are sensitive "
                "and omitted from activity details; fallback never happens implicitly."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=OPENAI_MANIFEST_PERMISSIONS,
            risk=RiskLevel.LOW,
            safe_to_retry=False,
            background_mode=BackgroundMode.FOREGROUND_ONLY,
        ).model_copy(update={"entry_point": f"{__name__}:OpenAISkill"})

    def set_offline_lock(self, enabled: bool) -> None:
        self.offline_lock = bool(enabled)
        self.adapter.set_offline_lock(self.offline_lock)
        if self.anthropic_adapter is not None:
            self.anthropic_adapter.set_offline_lock(self.offline_lock)

    def set_pricing(self, pricing: CloudPricing | None) -> None:
        self.pricing = pricing
        self.adapter.set_pricing(pricing)
        if self.anthropic_adapter is not None:
            self.anthropic_adapter.set_pricing(pricing)

    def set_provider_availability(self, availability: Mapping[str, bool]) -> None:
        self._provider_availability = {
            provider: bool(availability.get(provider, False))
            for provider in ("openai", "anthropic", "codex")
        }

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        permissions = list(
            OPENAI_CODEX_PERMISSIONS if args.provider == "codex" else OPENAI_NETWORK_PERMISSION
        )
        if args.fallback_provider == "anthropic" and self._provider_availability.get(
            "anthropic", False
        ):
            permissions.extend(ANTHROPIC_FALLBACK_PERMISSION)
        elif args.fallback_provider == "codex" and self._provider_availability.get("codex", False):
            permissions.extend(OPENAI_CODEX_PERMISSIONS)
        elif (
            args.fallback_provider == "openai"
            and args.provider == "codex"
            and self._provider_availability.get("openai", False)
        ):
            permissions.extend(OPENAI_NETWORK_PERMISSION)
        return _unique_permissions(permissions)

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.provider == "codex":
            model = args.model or self.default_codex_model
            label = f"local Codex using {model}"
        else:
            model = args.model or self.default_model
            label = f"OpenAI using {model}"
        if args.fallback_provider:
            fallback_model = (
                args.fallback_model
                or {
                    "openai": self.default_model,
                    "anthropic": self.default_anthropic_model,
                    "codex": self.default_codex_model,
                }[args.fallback_provider]
            )
            label += f"; explicit fallback {args.fallback_provider} using {fallback_model}"
        return f"Ask {label}; prompt content hidden for privacy."

    async def health(self) -> dict[str, object]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="OpenAI request cancelled.")
        model = args.model or (
            self.default_codex_model if args.provider == "codex" else self.default_model
        )
        try:
            execution = await execute_cloud_request(
                primary_provider=args.provider,
                primary_model=model,
                question=args.question,
                max_tokens=args.max_tokens,
                clients={
                    "openai": self.adapter,
                    "codex": self.codex_client,
                    "anthropic": self.anthropic_adapter,
                },
                fallback_provider=args.fallback_provider,
                fallback_model=args.fallback_model,
                default_models={
                    "openai": self.default_model,
                    "anthropic": self.default_anthropic_model,
                    "codex": self.default_codex_model,
                },
                cancel_event=cancel_event,
                offline_lock=self.offline_lock,
                available_providers=self._provider_availability,
            )
        except CloudExecutionFailure as failure:
            return cloud_failure_result(failure, provider=args.provider, model=model)
        output = OpenAIOutput.model_validate(cloud_output_fields(execution))
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=cloud_success_summary(execution),
            display_text=execution.answer.text,
            speech_text=execution.answer.text,
            data=_output_data(output),
            sensitive=True,
        )


OpenAiSkill = OpenAISkill


def _unique_permissions(permissions: list[PermissionScope]) -> tuple[PermissionScope, ...]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    result = []
    for permission in permissions:
        key = (permission.capability, permission.targets)
        if key in seen:
            continue
        seen.add(key)
        result.append(permission)
    return tuple(result)


def _output_data(output: OpenAIOutput) -> dict:
    data = output.model_dump(mode="json", exclude_none=True)
    if output.usage is None:
        data.pop("usage", None)
    if not output.attempts:
        data.pop("attempts", None)
    if not output.fallback_used:
        data.pop("fallback_used", None)
    return data
