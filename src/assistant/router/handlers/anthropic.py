from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


ANTHROPIC_SKILL = "integration.anthropic"


def _provider_alias(value: str) -> str:
    return {
        "chatgpt": "openai",
        "openai": "openai",
        "codex": "codex",
        "claude": "anthropic",
        "anthropic": "anthropic",
    }[value]


def _setup_route(status: str, *, allow_disconnected: bool = False) -> RouteResult | None:
    if status == "enabled":
        return None
    if allow_disconnected and status in {"disconnected", "reauthorization_required"}:
        return None
    if status == "disconnected":
        message = (
            "Anthropic is installed but no API key is connected. Open Extension Hub, select "
            "Anthropic, and choose Connect Anthropic."
        )
    elif status == "reauthorization_required":
        message = (
            "Anthropic needs attention before it can run. Open Extension Hub, select Anthropic, "
            "and reconnect the API key."
        )
    elif status == "disabled":
        message = (
            "Anthropic is installed but disabled. Open Skills, select Anthropic, and choose Enable."
        )
    else:
        message = "Anthropic is not installed. Open Extension Hub and install Anthropic first."
    return RouteResult(
        Intent(
            name="conversation.answer",
            arguments={"text": message},
            confidence=0.99,
            source="rule",
        )
    )


class AnthropicHandler:
    def route(self, context: RouteContext, router: DeterministicRouter) -> RouteResult | None:
        match = re.fullmatch(
            r"ask (claude|anthropic) "
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
                        name=ANTHROPIC_SKILL,
                        arguments={
                            "question": match.group(3),
                            "max_tokens": 1_500,
                            "fallback_provider": fallback,
                        },
                        confidence=0.99,
                        source="rule",
                    )
                )
                return (
                    _setup_route(
                        router.skill_availability(ANTHROPIC_SKILL),
                        allow_disconnected=True,
                    )
                    or result
                )

        match = re.fullmatch(r"ask (?:claude|anthropic) (?:for )?(.+)", context.routed)
        if match is None:
            return None
        result = RouteResult(
            Intent(
                name=ANTHROPIC_SKILL,
                arguments={"question": match.group(1), "max_tokens": 1_500},
                confidence=0.99,
                source="rule",
            )
        )
        return _setup_route(router.skill_availability(ANTHROPIC_SKILL)) or result
