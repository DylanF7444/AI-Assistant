from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


GOOGLE_CALENDAR_SKILL = "integration.google_calendar"


def _intent(operation: str, **arguments) -> RouteResult:
    return RouteResult(
        Intent(
            name=GOOGLE_CALENDAR_SKILL,
            arguments={"operation": operation, **arguments},
            confidence=0.98,
            source="rule",
        )
    )


def _clean_event_text(value: str) -> str:
    value = re.sub(r"^(?:an?|the)\s+", "", value.strip(), flags=re.IGNORECASE)
    return re.sub(r"[\s.?!]+$", "", value).strip()


def _strip_polite_prefix(value: str) -> str:
    return re.sub(
        r"^(?:(?:can|could|would|will)\s+you(?:\s+please)?|please)\s+",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    )


def _event_text(match: re.Match[str]) -> str:
    return _clean_event_text(
        " ".join(value for value in match.groups() if isinstance(value, str))
    )


def _day_start(value: datetime) -> datetime:
    return datetime.combine(value.date(), time.min, tzinfo=value.tzinfo)


def _event_window(text: str, now: datetime) -> tuple[datetime, datetime | None]:
    folded = text.casefold()
    today = _day_start(now)
    if "tomorrow" in folded:
        start = today + timedelta(days=1)
        return start, start + timedelta(days=1)
    if "today" in folded or "tonight" in folded:
        return now, today + timedelta(days=1)
    if "next week" in folded:
        days_until_monday = (7 - today.weekday()) or 7
        start = today + timedelta(days=days_until_monday)
        return start, start + timedelta(days=7)
    if "this week" in folded:
        return now, today + timedelta(days=7 - today.weekday())
    if re.search(r"\b(?:next|coming)\s+(?:seven|7)\s+days?\b", folded):
        return now, now + timedelta(days=7)
    return now, None


def _list_event_arguments(text: str, router: DeterministicRouter) -> dict[str, object]:
    start, end = _event_window(text, router.now())
    arguments: dict[str, object] = {
        "calendar_id": "primary",
        "time_min": start.isoformat(),
        "max_results": 10,
        "single_events": True,
        "order_by": "startTime",
    }
    if end is not None:
        arguments["time_max"] = end.isoformat()
    return arguments


def _setup_route(status: str) -> RouteResult | None:
    if status == "enabled":
        return None
    if status == "disconnected":
        message = (
            "Google Calendar is installed but no account is connected. Open Extension Hub, "
            "select Google Calendar, choose Connect Account, then Sign in with Google."
        )
    elif status == "disabled":
        message = (
            "Google Calendar is installed but disabled. Open Skills, select Google "
            "Calendar, and choose Enable."
        )
    else:
        message = (
            "Google Calendar is not installed. Open Extension Hub, install Google Calendar, "
            "then connect your account."
        )
    return RouteResult(
        Intent(
            name="conversation.answer",
            arguments={"text": message},
            confidence=0.99,
            source="rule",
        )
    )


class CalendarHandler:
    """Route common Calendar requests without requiring an LLM."""

    _CALENDAR = r"(?:my\s+|the\s+)?(?:google\s+)?calendar"

    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        raw = _strip_polite_prefix(re.sub(r"[.?!]+$", "", context.text.strip()))
        routed = _strip_polite_prefix(context.routed)
        result = self._calendar_intent(raw, routed, router)
        if result is None:
            return None
        setup = _setup_route(router.skill_availability(GOOGLE_CALENDAR_SKILL))
        return setup or result

    def _calendar_intent(
        self,
        raw: str,
        routed: str,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        if re.fullmatch(
            r"(?:show|list|what are)\s+(?:all\s+)?(?:my\s+)?"
            r"(?:google\s+)?calendars?",
            routed,
        ):
            return _intent("list_calendars", max_results=50)

        if match := re.fullmatch(
            r"(?:create|make)\s+(?:a\s+)?(?:new\s+)?(?:google\s+)?calendar"
            r"(?:\s+(?:called|named))?\s+(.+)",
            raw,
            flags=re.IGNORECASE,
        ):
            return _intent("create_calendar", summary=_clean_event_text(match.group(1)))

        create_patterns = (
            rf"(?:please\s+)?(?:add|put)\s+(.+?)\s+(?:to|on|in)\s+{self._CALENDAR}"
            r"(?:\s+(.+))?",
            rf"(?:please\s+)?(?:add|put)\s+(?:to|on|in)\s+{self._CALENDAR}"
            rf"(?:\s*[:,-]\s*|\s+)(.+)",
            rf"(?:please\s+)?(?:schedule|book)\s+(.+?)\s+(?:on|in)\s+{self._CALENDAR}"
            r"(?:\s+(.+))?",
            r"(?:please\s+)?(?:create|add|schedule|book)\s+(?:an?\s+)?"
            r"(?:calendar\s+)?(?:event|appointment|meeting)"
            r"(?:\s+(?:called|named|for))?\s+(.+)",
        )
        for pattern in create_patterns:
            if match := re.fullmatch(pattern, raw, flags=re.IGNORECASE):
                event_text = _event_text(match)
                if event_text:
                    return _intent(
                        "quick_add",
                        calendar_id="primary",
                        quick_add_text=event_text,
                    )

        if re.fullmatch(
            rf"(?:please\s+)?(?:add|put|schedule|book)(?:\s+(?:an?\s+)?"
            rf"(?:event|appointment|meeting))?\s+(?:to|on|in)\s+{self._CALENDAR}",
            routed,
        ):
            return RouteResult(
                None,
                clarification="What event should I add to your calendar, including its date and time?",
            )

        search_patterns = (
            rf"(?:find|search|look\s+for)\s+(.+?)\s+(?:on|in)\s+{self._CALENDAR}",
            rf"(?:find|search)\s+{self._CALENDAR}\s+(?:for\s+)?(.+)",
        )
        for pattern in search_patterns:
            if match := re.fullmatch(pattern, raw, flags=re.IGNORECASE):
                query = _clean_event_text(match.group(1))
                if query:
                    return _intent(
                        "list_events",
                        calendar_id="primary",
                        query=query,
                        max_results=50,
                        single_events=True,
                    )

        read_patterns = (
            rf"(?:check|show|view|read|open)\s+(?:me\s+)?{self._CALENDAR}"
            r"(?:\s+(?:for|on|today|tomorrow|tonight|this|next|coming).*)?",
            rf"what(?:'s|\s+is)\s+(?:on|in)\s+{self._CALENDAR}.*",
            rf"what(?:'s|\s+is)\s+(?:coming\s+up|scheduled)\s+"
            rf"(?:on|in)\s+{self._CALENDAR}.*",
            rf"show\s+me\s+what(?:'s|\s+is)\s+(?:coming\s+up|scheduled)\s+"
            rf"(?:on|in)\s+{self._CALENDAR}.*",
            rf"what\s+do\s+i\s+have\s+(?:on|in)\s+{self._CALENDAR}.*",
            rf"do\s+i\s+have\s+(?:anything|any\s+(?:events?|meetings?|appointments?))"
            rf"(?:\s+(?:on|in))?\s+{self._CALENDAR}.*",
            r"(?:show|list|check)\s+(?:my\s+)?(?:upcoming\s+)?"
            r"(?:calendar\s+)?(?:events?|meetings?|appointments?|agenda|schedule)"
            r"(?:\s+(?:today|tomorrow|tonight|this week|next week|"
            r"(?:next|coming)\s+(?:seven|7)\s+days?))?",
            r"what\s+(?:events?|meetings?|appointments?)\s+do\s+i\s+have"
            r"(?:\s+(?:today|tomorrow|tonight|this week|next week|"
            r"(?:next|coming)\s+(?:seven|7)\s+days?))?",
        )
        if any(re.fullmatch(pattern, routed) for pattern in read_patterns):
            return _intent("list_events", **_list_event_arguments(routed, router))
        return None
