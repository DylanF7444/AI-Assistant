from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


ANTHROPIC_SKILL = "integration.anthropic"


def _setup_route(status: str) -> RouteResult | None:
    if status == "enabled":
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
        message = "Anthropic is installed but disabled. Open Skills, select Anthropic, and choose Enable."
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
