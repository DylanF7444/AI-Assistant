from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


OUTLOOK_CALENDAR_SKILL = "integration.outlook_calendar"


def _intent(operation: str, **arguments) -> RouteResult:
    return RouteResult(
        Intent(
            name=OUTLOOK_CALENDAR_SKILL,
            arguments={"operation": operation, **arguments},
            confidence=0.98,
            source="rule",
        )
    )


def _setup_route(status: str) -> RouteResult | None:
    if status == "enabled":
        return None
    if status == "reauthorization_required":
        message = (
            "Outlook Calendar is connected but needs additional account access. Open Extension "
            "Hub, select Outlook Calendar, and manage the connected Microsoft account."
        )
    elif status == "disconnected":
        message = (
            "Outlook Calendar is installed but no Microsoft account is connected. Open Extension "
            "Hub, select Outlook Calendar, choose Connect Account, then sign in with Microsoft."
        )
    elif status == "disabled":
        message = "Outlook Calendar is installed but disabled. Open Skills, select Outlook Calendar, and choose Enable."
    else:
        message = "Outlook Calendar is not installed. Open Extension Hub and install Outlook Calendar first."
    return RouteResult(
        Intent(
            name="conversation.answer", arguments={"text": message}, confidence=0.99, source="rule"
        )
    )


class OutlookCalendarHandler:
    """Route explicit safe-path Outlook Calendar reads without an LLM."""

    def route(self, context: RouteContext, router: DeterministicRouter) -> RouteResult | None:
        raw = re.sub(r"[.?!]+$", "", context.text.strip())
        result = self._intent_for(raw, context.routed)
        if result is None:
            return None
        return _setup_route(router.skill_availability(OUTLOOK_CALENDAR_SKILL)) or result

    @staticmethod
    def _intent_for(raw: str, routed: str) -> RouteResult | None:
        search_patterns = (
            r"(?:find|search)\s+(?:my\s+)?outlook\s+calendar\s+(?:for\s+)?(.+)",
            r"(?:find|search)\s+(.+?)\s+(?:in|on)\s+(?:my\s+)?outlook\s+calendar",
        )
        for pattern in search_patterns:
            if match := re.fullmatch(pattern, raw, flags=re.IGNORECASE):
                query = match.group(1).strip(" \"'")
                if query:
                    return _intent("search_events", query=query, max_results=20)

        read_patterns = (
            r"(?:check|show|view|read|list)(?:\s+me)?\s+(?:my\s+)?outlook\s+calendar",
            r"what(?:'s|s|\s+is)\s+(?:in|on)\s+(?:my\s+)?outlook\s+calendar",
        )
        if any(re.fullmatch(pattern, routed) for pattern in read_patterns):
            return _intent("list_events", max_results=20)
        return None
