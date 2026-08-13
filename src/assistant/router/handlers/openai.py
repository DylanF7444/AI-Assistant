from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


OPENAI_SKILL = "integration.openai"


def _setup_route(status: str, *, provider: str) -> RouteResult | None:
    if status == "enabled":
        return None
    if provider == "codex" and status in {"disconnected", "reauthorization_required"}:
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


class OpenAIHandler:
    def route(self, context: RouteContext, router: DeterministicRouter) -> RouteResult | None:
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
        return _setup_route(
            router.skill_availability(OPENAI_SKILL),
            provider="openai",
        ) or result
