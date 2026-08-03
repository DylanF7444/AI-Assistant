from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


SLACK_SKILL = "integration.slack"


def _intent(operation: str, **arguments) -> RouteResult:
    return RouteResult(
        Intent(
            name=SLACK_SKILL,
            arguments={"operation": operation, **arguments},
            confidence=0.98,
            source="rule",
        )
    )


def _setup_route(status: str) -> RouteResult | None:
    if status == "enabled":
        return None
    if status == "disconnected":
        message = (
            "Slack is installed but no token is connected. Open Extension Hub, select Slack, "
            "choose Connect Account, and enter a read-only Slack user token."
        )
    elif status == "disabled":
        message = "Slack is installed but disabled. Open Skills, select Slack, and choose Enable."
    elif status == "reauthorization_required":
        message = "Slack needs a new token or additional read access. Open Extension Hub and manage Slack."
    else:
        message = "Slack is not installed. Open Extension Hub and install Slack first."
    return RouteResult(
        Intent(name="conversation.answer", arguments={"text": message}, confidence=0.99, source="rule")
    )


class SlackHandler:
    """Route explicit read-only Slack requests without an LLM."""

    def route(self, context: RouteContext, router: DeterministicRouter) -> RouteResult | None:
        raw = re.sub(r"[.?!]+$", "", context.text.strip())
        result = self._intent_for(raw, context.routed)
        if result is None:
            return None
        return _setup_route(router.skill_availability(SLACK_SKILL)) or result

    @staticmethod
    def _intent_for(raw: str, routed: str) -> RouteResult | None:
        patterns = (
            r"(?:find|search)\s+(?:my\s+)?slack\s+(?:for\s+)?(.+)",
            r"(?:find|search)\s+(.+?)\s+(?:in|on)\s+(?:my\s+)?slack",
        )
        for pattern in patterns:
            if match := re.fullmatch(pattern, raw, flags=re.IGNORECASE):
                query = match.group(1).strip(" \"'")
                if query:
                    return _intent("search_messages", query=query, max_results=25)
        if re.fullmatch(r"(?:show|list|check)\s+(?:my\s+)?slack\s+(?:channels?|conversations?)", routed):
            return _intent("list_conversations", max_results=25)
        if re.fullmatch(r"(?:show|list|check)\s+(?:my\s+)?slack", routed):
            return RouteResult(None, clarification="Should I search Slack messages or list Slack conversations?")
        return None
