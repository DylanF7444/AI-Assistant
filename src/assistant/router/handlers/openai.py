from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


OPENAI_SKILL = "integration.openai"


def _setup_route(
    status: str,
    *,
    provider: str,
    allow_disconnected: bool = False,
) -> RouteResult | None:
    if status == "enabled":
        return None
    if (provider == "codex" or allow_disconnected) and status in {
        "disconnected",
        "reauthorization_required",
    }:
        return None
    if status == "disconnected":
        message = (
            "OpenAI is installed but no API key is connected. Open Extension Hub, select "
            "OpenAI, and choose Connect OpenAI."
        )
    elif status == "reauthorization_required":
        message = (
            "OpenAI needs attention before it can run. Open Extension Hub, select OpenAI, "
            "and reconnect the API key."
        )
    elif status == "disabled":
        message = (
            "OpenAI, including local Codex mode, is installed but disabled. Open Skills, "
            "select OpenAI, and choose Enable."
        )
    else:
        message = (
            "OpenAI, including local Codex mode, is not installed. Open Extension Hub and "
            "install OpenAI first."
        )
    return RouteResult(
        Intent(
            name="conversation.answer",
            arguments={"text": message},
            confidence=0.99,
            source="rule",
        )
    )


def _provider_alias(value: str) -> str:
    return {
        "chatgpt": "openai",
        "openai": "openai",
        "codex": "codex",
        "claude": "anthropic",
        "anthropic": "anthropic",
    }[value]


class OpenAIHandler:
    def route(self, context: RouteContext, router: DeterministicRouter) -> RouteResult | None:
        match = re.fullmatch(
            r"ask (openai|chatgpt|codex) "
            r"(?:or|then|if unavailable (?:use|ask)|and then) "
            r"(openai|chatgpt|codex|claude|anthropic)"
            r"(?: (?:for|to))? (.+)",
            context.routed,
        )
        if match is not None:
            primary = _provider_alias(match.group(1))
            fallback = _provider_alias(match.group(2))
            if primary != fallback:
                result = RouteResult(
                    Intent(
                        name=OPENAI_SKILL,
                        arguments={
                            "provider": primary,
                            "question": match.group(3),
                            "max_tokens": 1_500,
                            "fallback_provider": fallback,
                        },
                        confidence=0.99,
                        source="rule",
                    )
                )
                status = router.skill_availability(
                    OPENAI_SKILL,
                    connection_required=primary != "codex",
                )
                return (
                    _setup_route(
                        status,
                        provider=primary,
                        allow_disconnected=True,
                    )
                    or result
                )

        match = re.fullmatch(r"ask codex (?:for )?(.+)", context.routed)
        if match is not None:
            result = RouteResult(
                Intent(
                    name=OPENAI_SKILL,
                    arguments={
                        "provider": "codex",
                        "question": match.group(1),
                        "max_tokens": 1_500,
                    },
                    confidence=0.99,
                    source="rule",
                )
            )
            return (
                _setup_route(
                    router.skill_availability(OPENAI_SKILL, connection_required=False),
                    provider="codex",
                )
                or result
            )

        match = re.fullmatch(
            r"(?:make|write|generate|create) (?:some )?(?:code|a script|an app|a program)"
            r"(?: (?:to|for|that|which))? (.+)",
            context.routed,
        )
        if match is not None:
            result = RouteResult(
                Intent(
                    name=OPENAI_SKILL,
                    arguments={
                        "provider": "codex",
                        "question": f"Write code {match.group(1)}",
                        "max_tokens": 4_000,
                    },
                    confidence=0.96,
                    source="rule",
                )
            )
            return (
                _setup_route(
                    router.skill_availability(OPENAI_SKILL, connection_required=False),
                    provider="codex",
                )
                or result
            )

        match = re.fullmatch(r"code (?:up|me|for me) (.+)", context.routed)
        if match is not None:
            result = RouteResult(
                Intent(
                    name=OPENAI_SKILL,
                    arguments={
                        "provider": "codex",
                        "question": f"Write code for {match.group(1)}",
                        "max_tokens": 4_000,
                    },
                    confidence=0.94,
                    source="rule",
                )
            )
            return (
                _setup_route(
                    router.skill_availability(OPENAI_SKILL, connection_required=False),
                    provider="codex",
                )
                or result
            )

        match = re.fullmatch(r"ask (?:chatgpt|openai) (?:for )?(.+)", context.routed)
        if match is None:
            return None
        result = RouteResult(
            Intent(
                name=OPENAI_SKILL,
                arguments={"question": match.group(1), "max_tokens": 1_500},
                confidence=0.99,
                source="rule",
            )
        )
        return (
            _setup_route(
                router.skill_availability(OPENAI_SKILL),
                provider="openai",
            )
            or result
        )
