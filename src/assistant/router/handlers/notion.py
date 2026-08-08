from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


NOTION_SKILL = "integration.notion"


def _intent(operation: str, **arguments) -> RouteResult:
    return RouteResult(
        Intent(
            name=NOTION_SKILL,
            arguments={"operation": operation, **arguments},
            confidence=0.98,
            source="rule",
        )
    )


def _setup_route(status: str) -> RouteResult | None:
    if status == "enabled":
        return None
    if status == "disconnected":
        message = "Notion is installed but no token is connected. Open Extension Hub, select Notion, and connect a token with the reviewed page scopes."
    elif status == "disabled":
        message = "Notion is installed but disabled. Open Skills, select Notion, and choose Enable."
    elif status == "reauthorization_required":
        message = "Notion needs a new token or additional page access. Open Extension Hub and manage Notion."
    else:
        message = "Notion is not installed. Open Extension Hub and install Notion first."
    return RouteResult(
        Intent(
            name="conversation.answer", arguments={"text": message}, confidence=0.99, source="rule"
        )
    )


class NotionHandler:
    def route(self, context: RouteContext, router: DeterministicRouter) -> RouteResult | None:
        raw = re.sub(r"[.?!]+$", "", context.text.strip())
        result = self._intent_for(raw, context.routed)
        if result is None:
            return None
        return _setup_route(router.skill_availability(NOTION_SKILL)) or result

    @staticmethod
    def _intent_for(raw: str, routed: str) -> RouteResult | None:
        for pattern in (
            r"(?:find|search)\s+(?:my\s+)?notion\s+(?:for\s+)?(.+)",
            r"(?:find|search)\s+(.+?)\s+(?:in|on)\s+(?:my\s+)?notion",
        ):
            if match := re.fullmatch(pattern, raw, flags=re.IGNORECASE):
                query = match.group(1).strip(" \"'")
                if query:
                    return _intent("search", query=query, page_size=25)
        if re.fullmatch(r"(?:search|find|show|list)\s+(?:my\s+)?notion", routed):
            return RouteResult(
                None, clarification="What page or data source should I search for in Notion?"
            )
        return None
