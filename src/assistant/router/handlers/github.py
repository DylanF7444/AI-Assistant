from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


GITHUB_SKILL = "integration.github"


def _intent(operation: str, **arguments) -> RouteResult:
    return RouteResult(
        Intent(
            name=GITHUB_SKILL,
            arguments={"operation": operation, **arguments},
            confidence=0.98,
            source="rule",
        )
    )


def _setup_route(status: str) -> RouteResult | None:
    if status == "enabled":
        return None
    if status == "disconnected":
        message = "GitHub is installed but no token is connected. Open Extension Hub, select GitHub, and connect a read-only token."
    elif status == "disabled":
        message = "GitHub is installed but disabled. Open Skills, select GitHub, and choose Enable."
    elif status == "reauthorization_required":
        message = "GitHub needs a new token or additional repository access. Open Extension Hub and manage GitHub."
    else:
        message = "GitHub is not installed. Open Extension Hub and install GitHub first."
    return RouteResult(Intent(name="conversation.answer", arguments={"text": message}, confidence=0.99, source="rule"))


class GitHubHandler:
    def route(self, context: RouteContext, router: DeterministicRouter) -> RouteResult | None:
        raw = re.sub(r"[.?!]+$", "", context.text.strip())
        result = self._intent_for(raw, context.routed)
        if result is None:
            return None
        return _setup_route(router.skill_availability(GITHUB_SKILL)) or result

    @staticmethod
    def _intent_for(raw: str, routed: str) -> RouteResult | None:
        for pattern in (
            r"(?:find|search)\s+(?:my\s+)?github\s+(?:for\s+)?(.+)",
            r"(?:find|search)\s+(.+?)\s+(?:on|in)\s+(?:my\s+)?github",
        ):
            if match := re.fullmatch(pattern, raw, flags=re.IGNORECASE):
                query = match.group(1).strip(" \"'")
                if query:
                    return _intent("search_repositories", query=query, max_results=25)
        if re.fullmatch(r"(?:show|list|check)\s+(?:my\s+)?github\s+(?:repositories|repos)", routed):
            return _intent("list_repositories", max_results=25)
        if match := re.fullmatch(
            r"(?:show|list|find)\s+(?:the\s+)?issues\s+in\s+(?P<owner>[^/\s]+)/(?P<repo>[^\s]+)",
            raw,
            flags=re.IGNORECASE,
        ):
            return _intent("list_issues", owner=match.group("owner"), repo=match.group("repo"), max_results=25)
        if re.fullmatch(r"(?:search|find|show|list)\s+(?:my\s+)?github", routed):
            return RouteResult(None, clarification="Should I search GitHub repositories or list your repositories?")
        return None
