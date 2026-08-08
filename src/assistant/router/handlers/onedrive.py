from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


ONEDRIVE_SKILL = "integration.onedrive"


def _intent(operation: str, **arguments) -> RouteResult:
    return RouteResult(
        Intent(
            name=ONEDRIVE_SKILL,
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
            "OneDrive is connected but needs additional account access. Open Extension Hub, "
            "select OneDrive, and manage the connected Microsoft account."
        )
    elif status == "disconnected":
        message = (
            "OneDrive is installed but no Microsoft account is connected. Open Extension Hub, "
            "select OneDrive, choose Connect Account, then sign in with Microsoft."
        )
    elif status == "disabled":
        message = (
            "OneDrive is installed but disabled. Open Skills, select OneDrive, and choose Enable."
        )
    else:
        message = "OneDrive is not installed. Open Extension Hub and install OneDrive first."
    return RouteResult(
        Intent(
            name="conversation.answer", arguments={"text": message}, confidence=0.99, source="rule"
        )
    )


class OneDriveHandler:
    """Route explicit deterministic OneDrive discovery requests without an LLM."""

    def route(self, context: RouteContext, router: DeterministicRouter) -> RouteResult | None:
        raw = re.sub(r"[.?!]+$", "", context.text.strip())
        result = self._intent_for(raw, context.routed)
        if result is None:
            return None
        return _setup_route(router.skill_availability(ONEDRIVE_SKILL)) or result

    @staticmethod
    def _intent_for(raw: str, routed: str) -> RouteResult | None:
        search_patterns = (
            r"(?:find|search)\s+(?:my\s+)?onedrive\s+(?:for\s+)?(.+)",
            r"(?:find|search)\s+(.+?)\s+(?:in|on)\s+(?:my\s+)?onedrive",
        )
        for pattern in search_patterns:
            if match := re.fullmatch(pattern, raw, flags=re.IGNORECASE):
                query = match.group(1).strip(" \"'")
                if query:
                    return _intent("search", query=query, max_results=25)

        if re.fullmatch(r"(?:show|list)\s+(?:my\s+)?onedrive\s+files?", routed):
            return _intent("list_children", file_id="root", max_results=25)
        if re.fullmatch(r"(?:show|list|check|search)\s+(?:my\s+)?onedrive(?:\s+files?)?", routed):
            return RouteResult(
                None,
                clarification="What file or folder should I search for in OneDrive?",
            )
        return None
