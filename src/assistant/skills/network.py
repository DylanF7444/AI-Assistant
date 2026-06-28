from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from assistant.contracts import PermissionScope, ResultStatus, SkillCall, SkillResult
from assistant.integrations.web import CloudModelClient, CodexExecClient, WebClient
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest


NETWORK_PERMISSION = (
    PermissionScope(
        capability="network",
        targets=(),
        reason="Connect to an external internet service.",
    ),
)

CODEX_EXEC_PERMISSION = (
    *NETWORK_PERMISSION,
    PermissionScope(
        capability="process.launch",
        targets=("codex",),
        reason="Start the Codex CLI to ask an external AI model.",
    ),
)


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    count: int = Field(default=5, ge=1, le=10)


class WebSearchOutput(BaseModel):
    results: list[dict[str, str]]


class WebSearchSkill(Skill):
    input_model = WebSearchInput
    output_model = WebSearchOutput

    def __init__(
        self,
        client: WebClient,
        key: Callable[[str], str | None],
        provider: str = "duckduckgo",
    ) -> None:
        self.client = client
        self.key = key
        self.provider = provider
        self.manifest = manifest(
            skill_id="web.search",
            display_name="Web Search",
            description=(
                "Search the current public internet using DuckDuckGo Instant "
                "Answer or Brave Search."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=NETWORK_PERMISSION,
            safe_to_retry=True,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        api_key = None
        if self.provider == "brave":
            api_key = self.key("brave") or os.getenv("BRAVE_SEARCH_API_KEY")
        results = await self.client.search(
            args.query,
            provider=self.provider,
            api_key=api_key,
            count=args.count,
        )
        lines = [
            f"{index}. {item['title']}\n{item['url']}\n{item['description']}"
            for index, item in enumerate(results, 1)
        ]
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Found {len(results)} web results.",
            display_text="\n\n".join(lines),
            data=WebSearchOutput(results=results).model_dump(),
        )


class WebFetchInput(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    max_characters: int = Field(default=12000, ge=500, le=50000)


class WebFetchOutput(BaseModel):
    url: str
    text: str


class WebFetchSkill(Skill):
    input_model = WebFetchInput
    output_model = WebFetchOutput

    def __init__(self, client: WebClient) -> None:
        self.client = client
        self.manifest = manifest(
            skill_id="web.fetch",
            display_name="Web Fetch",
            description="Read text from a specific public HTTPS web page.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=NETWORK_PERMISSION,
            safe_to_retry=True,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        result = await self.client.fetch(args.url, args.max_characters)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Read {len(result['text'])} characters from the page.",
            display_text=result["text"],
            data=WebFetchOutput(**result).model_dump(),
        )


class CloudAskInput(BaseModel):
    provider: Literal["openai", "codex", "anthropic"]
    question: str = Field(min_length=1, max_length=20000)
    model: str | None = Field(default=None, max_length=200)
    max_tokens: int = Field(default=1500, ge=1, le=16000)


class CloudAskOutput(BaseModel):
    provider: str
    model: str
    answer: str


class CloudAskSkill(Skill):
    input_model = CloudAskInput
    output_model = CloudAskOutput

    def __init__(
        self,
        client: CloudModelClient,
        key: Callable[[str], str | None],
        default_models: dict[str, str],
        codex_client: CodexExecClient | None = None,
    ) -> None:
        self.client = client
        self.key = key
        self.default_models = default_models
        self.codex_client = codex_client or CodexExecClient()
        self.manifest = manifest(
            skill_id="cloud.ask",
            display_name="Cloud Ask",
            description=(
                "Ask ChatGPT/OpenAI or Anthropic Claude using cloud APIs, or ask "
                "Codex through the local codex exec CLI."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=NETWORK_PERMISSION,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return CODEX_EXEC_PERMISSION if args.provider == "codex" else NETWORK_PERMISSION

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if args.provider == "codex":
            model = args.model or self.default_models[args.provider]
            answer = await self.codex_client.ask(
                args.question,
                model=model,
                cancel_event=cancel_event,
            )
            return SkillResult(
                status=ResultStatus.COMPLETED,
                summary=f"Codex answered using {model}.",
                display_text=answer,
                speech_text=answer,
                data=CloudAskOutput(
                    provider=args.provider, model=model, answer=answer
                ).model_dump(),
                sensitive=True,
            )
        api_key = self.key(args.provider) or os.getenv(
            "OPENAI_API_KEY" if args.provider == "openai" else "ANTHROPIC_API_KEY"
        )
        if not api_key:
            raise RuntimeError(f"An {args.provider.title()} API key is not configured")
        model = args.model or self.default_models[args.provider]
        answer = await self.client.ask(
            args.provider, api_key, model, args.question, args.max_tokens
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"{args.provider.title()} answered using {model}.",
            display_text=answer,
            speech_text=answer,
            data=CloudAskOutput(
                provider=args.provider, model=model, answer=answer
            ).model_dump(),
            sensitive=True,
        )
