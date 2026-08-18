from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    RequestResultItem,
    ResultStatus,
    SkillCall,
    SkillResult,
)
from assistant.integrations.cloud import CloudOfflineError
from assistant.integrations.headless_browser import HeadlessBrowser
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
                "Search the current public internet for up-to-date pages, articles, "
                "sources, and research using DuckDuckGo or Brave Search."
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
            items=tuple(
                RequestResultItem(
                    item_id=f"web-{index}",
                    title=item["title"] or item["url"],
                    value=item["url"],
                    kind="web_page",
                    description=item["description"][:2_000] or None,
                    metadata={"url": item["url"]},
                )
                for index, item in enumerate(results, 1)
            ),
        )


class NewsSearchSkill(Skill):
    input_model = WebSearchInput
    output_model = WebSearchOutput

    def __init__(self, client: WebClient) -> None:
        self.client = client
        self.manifest = manifest(
            skill_id="web.news_search",
            display_name="News Search",
            description=(
                "Find current news headlines with source and publication time for "
                "latest-news questions, briefings, digests, reports, and recaps."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=NETWORK_PERMISSION,
            safe_to_retry=True,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        results = await self.client.search_news(args.query, count=args.count)
        lines = [
            f"{index}. {item['title']}\n{item['url']}\n{item['description']}"
            for index, item in enumerate(results, 1)
        ]
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Found {len(results)} current news headlines.",
            display_text="\n\n".join(lines),
            data=WebSearchOutput(results=results).model_dump(),
            items=tuple(
                RequestResultItem(
                    item_id=f"news-{index}",
                    title=item["title"] or item["url"],
                    value=item["url"],
                    kind="news_article",
                    description=item["description"][:2_000] or None,
                    metadata={"url": item["url"]},
                )
                for index, item in enumerate(results, 1)
            ),
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


class HeadlessReadInput(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    max_characters: int = Field(default=12000, ge=500, le=50000)
    max_links: int = Field(default=30, ge=0, le=100)
    timeout_seconds: float = Field(default=30, ge=5, le=60)
    wait_after_load_ms: int = Field(default=250, ge=0, le=5000)
    extract: str | None = Field(default=None, min_length=1, max_length=200)
    max_items: int = Field(default=5, ge=1, le=20)


class HeadlessReadOutput(BaseModel):
    url: str
    title: str
    text: str
    links: list[dict[str, str]]
    items: list[dict[str, str]]


class HeadlessReadSkill(Skill):
    input_model = HeadlessReadInput
    output_model = HeadlessReadOutput

    def __init__(self, browser: HeadlessBrowser) -> None:
        self.browser = browser
        self.manifest = manifest(
            skill_id="web.headless_read",
            display_name="Headless Web Read",
            description=(
                "Browse and render a public HTTPS website in a private headless browser, "
                "then return visible text and links or extract matching page items such "
                "as the first deal, product, article, listing, or search result."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=NETWORK_PERMISSION,
            safe_to_retry=True,
            background_mode=BackgroundMode.SAFE,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return (
            PermissionScope(
                capability="network",
                targets=(args.url,),
                reason="Load this public HTTPS page in an isolated headless browser.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        return f"Read {args.url} in an isolated headless browser"

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(
                status=ResultStatus.CANCELLED, summary="Headless browsing cancelled."
            )
        page = await self.browser.read_page(
            args.url,
            max_characters=args.max_characters,
            max_links=args.max_links,
            timeout_seconds=args.timeout_seconds,
            wait_after_load_ms=args.wait_after_load_ms,
            extract=args.extract,
            max_items=args.max_items,
        )
        output = HeadlessReadOutput(
            url=page.url,
            title=page.title,
            text=page.text,
            links=list(page.links),
            items=list(page.items),
        )
        if page.items:
            item_lines = []
            for index, item in enumerate(page.items, 1):
                title = item["title"] or item["url"]
                detail = item["text"]
                if detail.casefold().startswith(title.casefold()):
                    detail = detail[len(title) :].strip(" -:|")
                lines = [f"{index}. {title}"]
                if detail:
                    lines.append(detail)
                lines.append(item["url"])
                item_lines.append("\n".join(lines))
            display = f"Matching items from {page.title or page.url}:\n\n" + "\n\n".join(item_lines)
        else:
            display = page.text
        if page.links and not page.items:
            display += "\n\nLinks:\n" + "\n".join(
                f"- {item['text'] or item['url']}: {item['url']}" for item in page.links
            )
        summary = (
            f"Found {len(page.items)} matching page item"
            f"{'s' if len(page.items) != 1 else ''}: {page.items[0]['title']}"
            if page.items
            else f"Rendered {page.title or page.url} without opening a window."
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            display_text=display,
            data=output.model_dump(),
            items=tuple(
                RequestResultItem(
                    item_id=f"page-{index}",
                    title=item["title"] or item["url"],
                    value=item["url"],
                    kind="web_page",
                    description=item["text"][:2_000] or None,
                    metadata={"url": item["url"]},
                )
                for index, item in enumerate(page.items, 1)
            ),
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
    """Legacy direct-call compatibility for callers that still construct it explicitly.

    Bootstrap retains :class:`CodexAskSkill` for ``cloud.ask`` compatibility,
    while current Codex and OpenAI routes use the managed OpenAI Extension Hub
    skill.
    """

    input_model = CloudAskInput
    output_model = CloudAskOutput

    def __init__(
        self,
        client: CloudModelClient,
        key: Callable[[str], str | None],
        default_models: dict[str, str],
        codex_client: CodexExecClient | None = None,
        offline_lock: bool = False,
    ) -> None:
        self.client = client
        self.key = key
        self.default_models = default_models
        self.codex_client = codex_client or CodexExecClient()
        self.offline_lock = bool(offline_lock)
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
            background_mode=BackgroundMode.SAFE,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return CODEX_EXEC_PERMISSION if args.provider == "codex" else NETWORK_PERMISSION

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if self.offline_lock:
            error = CloudOfflineError(args.provider.title())
            return SkillResult(
                status=ResultStatus.FAILED,
                summary=error.user_message,
                display_text=error.user_message,
                speech_text=error.user_message,
                data={"provider": args.provider, "code": error.code},
                sensitive=True,
            )
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
            data=CloudAskOutput(provider=args.provider, model=model, answer=answer).model_dump(),
            sensitive=True,
        )


class CodexAskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["codex"] = "codex"
    question: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    max_tokens: int = Field(default=1_500, ge=1, le=16_000)


class CodexAskSkill(Skill):
    """The retained built-in path for the locally installed Codex CLI."""

    input_model = CodexAskInput
    output_model = CloudAskOutput

    def __init__(
        self,
        default_model: str,
        codex_client: CodexExecClient | None = None,
        offline_lock: bool = False,
    ) -> None:
        self.default_model = default_model
        self.codex_client = codex_client or CodexExecClient()
        self.offline_lock = bool(offline_lock)
        self.manifest = manifest(
            skill_id="cloud.ask",
            display_name="Codex Ask",
            description=(
                "Ask the locally installed Codex CLI to review, explain, or generate code in "
                "a read-only sandbox."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=CODEX_EXEC_PERMISSION,
            safe_to_retry=False,
            background_mode=BackgroundMode.FOREGROUND_ONLY,
        )

    def set_offline_lock(self, enabled: bool) -> None:
        self.offline_lock = bool(enabled)

    def required_permissions(self, call: SkillCall):
        self.validate(call.arguments)
        return CODEX_EXEC_PERMISSION

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="Codex request cancelled.")
        if self.offline_lock:
            error = CloudOfflineError("Codex")
            return SkillResult(
                status=ResultStatus.FAILED,
                summary=error.user_message,
                display_text=error.user_message,
                speech_text=error.user_message,
                data={"provider": "codex", "code": error.code},
                sensitive=True,
            )
        model = args.model or self.default_model
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
            data=CloudAskOutput(provider="codex", model=model, answer=answer).model_dump(),
            sensitive=True,
        )
