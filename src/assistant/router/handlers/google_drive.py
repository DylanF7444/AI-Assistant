from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


GOOGLE_DRIVE_SKILL = "integration.google_drive"


def _intent(operation: str, **arguments) -> RouteResult:
    return RouteResult(
        Intent(
            name=GOOGLE_DRIVE_SKILL,
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
            "Google Drive is connected but needs additional access. Open Extension Hub, "
            "select Google Drive, and manage the connected account."
        )
    elif status == "disconnected":
        message = (
            "Google Drive is installed but no account is connected. Open Extension Hub, "
            "select Google Drive, choose Connect Account, then Sign in with Google."
        )
    elif status == "disabled":
        message = "Google Drive is installed but disabled. Open Skills, select Google Drive, and choose Enable."
    else:
        message = "Google Drive is not installed. Open Extension Hub, install Google Drive, then connect your account."
    return RouteResult(
        Intent(
            name="conversation.answer",
            arguments={"text": message},
            confidence=0.99,
            source="rule",
        )
    )


class GoogleDriveHandler:
    """Route explicit Google Drive discovery requests without an LLM."""

    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        raw = re.sub(r"[.?!]+$", "", context.text.strip())
        routed = context.routed
        result = self._intent_for(raw, routed)
        if result is None:
            return None
        return _setup_route(router.skill_availability(GOOGLE_DRIVE_SKILL)) or result

    @staticmethod
    def _intent_for(raw: str, routed: str) -> RouteResult | None:
        search_patterns = (
            r"(?:find|search)\s+(?:my\s+)?(?:google\s+)?drive\s+(?:for\s+)?(.+)",
            r"(?:find|search)\s+(.+?)\s+(?:in|on)\s+(?:my\s+)?(?:google\s+)?drive",
        )
        for pattern in search_patterns:
            if match := re.fullmatch(pattern, raw, flags=re.IGNORECASE):
                query = match.group(1).strip(" \"'")
                if query:
                    return _intent("search", query=query, max_results=25)

        if re.fullmatch(
            r"(?:show|list)\s+(?:my\s+)?(?:google\s+)?drive\s+files?",
            routed,
        ):
            return _intent("list_children", file_id="root", max_results=25)
        if re.fullmatch(
            r"(?:show|list|check|search)\s+(?:my\s+)?(?:google\s+)?drive(?:\s+files?)?",
            routed,
        ):
            return RouteResult(
                None,
                clarification="What file or folder should I search for in Google Drive?",
            )
        return None
