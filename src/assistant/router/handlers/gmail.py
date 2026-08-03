from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


GMAIL_SKILL = "integration.gmail"


def _intent(operation: str, **arguments) -> RouteResult:
    return RouteResult(
        Intent(
            name=GMAIL_SKILL,
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
            "Gmail is connected but needs additional account access. Open Extension Hub, "
            "select Gmail, and manage the connected account."
        )
    elif status == "disconnected":
        message = (
            "Gmail is installed but no account is connected. Open Extension Hub, "
            "select Gmail, choose Connect Account, then Sign in with Google."
        )
    elif status == "disabled":
        message = "Gmail is installed but disabled. Open Skills, select Gmail, and choose Enable."
    else:
        message = (
            "Gmail is not installed. Open Extension Hub, install Gmail, then connect your account."
        )
    return RouteResult(
        Intent(
            name="conversation.answer",
            arguments={"text": message},
            confidence=0.99,
            source="rule",
        )
    )


class GmailHandler:
    """Route common Gmail requests without requiring an LLM."""

    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        raw = re.sub(r"[.?!]+$", "", context.text.strip())
        routed = context.routed
        result = self._gmail_intent(raw, routed)
        if result is None:
            return None
        return _setup_route(router.skill_availability(GMAIL_SKILL)) or result

    @staticmethod
    def _gmail_intent(raw: str, routed: str) -> RouteResult | None:
        send_pattern = (
            r"(?:send|write|compose)\s+(?:an?\s+)?(?:email|message)\s+to\s+"
            r"(?P<to>[^\s,;]+@[^\s,;]+)\s+(?:with\s+)?subject\s+"
            r"(?P<subject>.+?)\s+(?:saying|with\s+(?:the\s+)?body|body)\s+"
            r"(?P<body>.+)"
        )
        if match := re.fullmatch(send_pattern, raw, flags=re.IGNORECASE):
            return _intent(
                "send_message",
                to=[match.group("to")],
                subject=match.group("subject").strip(" \"'"),
                body=match.group("body").strip(" \"'"),
            )

        draft_pattern = send_pattern.replace("(?:send|write|compose)", "(?:draft|create)")
        if match := re.fullmatch(draft_pattern, raw, flags=re.IGNORECASE):
            return _intent(
                "create_draft",
                to=[match.group("to")],
                subject=match.group("subject").strip(" \"'"),
                body=match.group("body").strip(" \"'"),
            )

        search_patterns = (
            r"(?:find|search)\s+(?:my\s+)?(?:gmail|email|inbox)\s+(?:for\s+)?(.+)",
            r"(?:find|search)\s+(?:for\s+)?(.+?)\s+(?:in|on)\s+(?:my\s+)?"
            r"(?:gmail|email|inbox)",
        )
        for pattern in search_patterns:
            if match := re.fullmatch(pattern, raw, flags=re.IGNORECASE):
                query = match.group(1).strip()
                if query:
                    return _intent("list_messages", query=query, max_results=20)

        if match := re.fullmatch(
            r"(?:show|find|list|read)\s+(?:me\s+)?(?:my\s+)?"
            r"(?:emails?|messages?)\s+from\s+(.+)",
            raw,
            flags=re.IGNORECASE,
        ):
            return _intent(
                "list_messages",
                query=f"from:{match.group(1).strip()}",
                max_results=20,
            )

        unread = r"(?:my\s+)?(?:new|unread)\s+(?:gmail\s+)?(?:emails?|messages?)"
        if re.fullmatch(rf"(?:check|show|list|read)(?:\s+me)?\s+{unread}", routed):
            return _intent("list_messages", query="in:inbox is:unread", max_results=20)

        read_patterns = (
            r"(?:check|show|view|read|list)(?:\s+me)?\s+(?:my\s+)?"
            r"(?:gmail|email|emails|inbox|messages)",
            r"what(?:'s|s|\s+is)\s+(?:in|on)\s+(?:my\s+)?"
            r"(?:gmail|email|inbox)",
            r"do\s+i\s+have\s+(?:any\s+)?(?:new|unread)\s+(?:emails?|messages?)",
        )
        if any(re.fullmatch(pattern, routed) for pattern in read_patterns):
            query = "in:inbox is:unread" if re.search(r"\b(?:new|unread)\b", routed) else "in:inbox"
            return _intent("list_messages", query=query, max_results=20)
        return None
