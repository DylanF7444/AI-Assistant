from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from assistant.contracts import Intent
from assistant.router.normalization import spoken_number
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


def _clock_time(value: str) -> str | None:
    value = value.strip().casefold().replace(".", "")
    value = re.sub(r"\b([ap])\s+m\b", r"\1m", value)
    match = re.fullmatch(
        r"(?P<hour>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve)(?::(?P<minute>\d{2}))?\s*(?P<period>am|pm)",
        value,
    )
    if match is None:
        return None
    try:
        hour = int(spoken_number(match.group("hour")))
    except ValueError:
        return None
    minute = int(match.group("minute") or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group("period") == "pm" and hour != 12:
        hour += 12
    elif match.group("period") == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}:00"


_DAILY_CADENCE = r"(?:every\s+day|everyday|each\s+day|daily|every\s+morning|each\s+morning)"
_CLOCK_VALUE = (
    r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"(?::\d{2})?\s*(?:a\.?\s*m\.?|p\.?\s*m\.?)"
)
_REMINDER_CADENCE = (
    r"day|everyday|daily|weekday|weekdays|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
)


def _duration_seconds(amount_text: str, unit: str) -> float | None:
    try:
        amount = spoken_number(amount_text)
    except ValueError:
        return None
    if amount <= 0:
        return None
    multiplier = {
        "second": 1,
        "seconds": 1,
        "minute": 60,
        "minutes": 60,
        "hour": 3600,
        "hours": 3600,
        "day": 86400,
        "days": 86400,
    }[unit]
    return amount * multiplier


def _daily_notification(message: str, clock_value: str) -> RouteResult:
    local_time = _clock_time(clock_value)
    if local_time is None:
        return RouteResult(
            None,
            clarification="What time should the daily message run, including AM or PM?",
        )
    message = message.strip()
    return RouteResult(
        Intent(
            name="core.schedule",
            arguments={
                "operation": "create",
                "name": message,
                "message": message,
                "recurrence": {
                    "frequency": "daily",
                    "interval": 1,
                    "local_time": local_time,
                },
            },
            confidence=0.99,
            source="rule",
        )
    )


def _daily_web_action(
    *,
    extract: str,
    clock_value: str,
    website: str,
    router: DeterministicRouter,
) -> RouteResult:
    local_time = _clock_time(clock_value)
    if local_time is None:
        return RouteResult(
            None,
            clarification="What time should the daily web update run, including AM or PM?",
        )
    website = website.strip()
    url = router._website_url(website)
    if url is None:
        return RouteResult(
            None,
            clarification="Which website should I use for the daily update?",
        )
    extract = extract.strip()
    if website.casefold() == "amazon":
        url = "https://www.amazon.com/s?" + urlencode({"k": f"daily {extract}"})
    name = f"Daily {website.title()} {extract}"[:200]
    return RouteResult(
        Intent(
            name="core.schedule",
            arguments={
                "operation": "create_action",
                "name": name,
                "recurrence": {
                    "frequency": "daily",
                    "interval": 1,
                    "local_time": local_time,
                },
                "skill_id": "web.headless_read",
                "arguments": {
                    "url": url,
                    "max_characters": 12000,
                    "max_links": 30,
                    "timeout_seconds": 30,
                    "wait_after_load_ms": 750,
                    "extract": extract,
                    "max_items": 5,
                },
                "delivery_mode": "always",
            },
            confidence=0.99,
            source="rule",
        )
    )


def _route_daily_web_action(
    routed: str,
    router: DeterministicRouter,
) -> RouteResult | None:
    if re.search(rf"\b{_DAILY_CADENCE}\b", routed) is None:
        return None
    if re.search(
        r"\b(?:show|give|get|find|send|display|check|monitor|watch|alert)\b",
        routed,
    ) is None:
        return None
    time_match = re.search(rf"\bat\s+(?P<time>{_CLOCK_VALUE})\b", routed)
    if time_match is None:
        return RouteResult(
            None,
            clarification="What time should the daily web update run, including AM or PM?",
        )

    website = next(
        (
            name
            for name in sorted(router.WEBSITE_APPLICATIONS, key=len, reverse=True)
            if re.search(rf"\b{re.escape(name)}\b", routed)
        ),
        None,
    )
    if website is None:
        source_match = re.search(
            r"\b(?:from|on)\s+(?P<website>https://\S+|[a-z0-9.-]+\.[a-z]{2,})\b",
            routed,
        )
        website = source_match.group("website") if source_match else None
    if website is None:
        return RouteResult(
            None,
            clarification="Which website should I use for the daily update?",
        )

    extract = re.sub(
        r"\b(?:show|give|get|find|send|display|check|monitor|watch|alert)"
        r"\s+(?:me\s+)?",
        " ",
        routed,
        count=1,
    )
    extract = re.sub(rf"\b{_DAILY_CADENCE}\b", " ", extract)
    extract = re.sub(rf"\bat\s+{_CLOCK_VALUE}\b", " ", extract)
    extract = re.sub(
        rf"\b(?:from|on)\s+(?:the\s+)?{re.escape(website)}\b",
        " ",
        extract,
    )
    extract = re.sub(rf"\b{re.escape(website)}\b", " ", extract)
    extract = re.sub(r"\b(?:from|on)\b", " ", extract)
    extract = re.sub(r"[.,;:]+", " ", extract)
    extract = " ".join(extract.split()).strip(" -")
    if not extract:
        return RouteResult(
            None,
            clarification=f"What should I retrieve from {website.title()} each day?",
        )
    return _daily_web_action(
        extract=extract,
        clock_value=time_match.group("time"),
        website=website,
        router=router,
    )


class ProductivityHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        if playbook_route := self._route_playbook_command(context):
            return playbook_route
        daily_message_patterns = (
            rf"(?:say|announce)\s+(?P<message>.+?)\s+{_DAILY_CADENCE}\s+at\s+(?P<time>.+)",
            rf"(?:say|announce)\s+(?P<message>.+?)\s+at\s+(?P<time>.+?)\s+{_DAILY_CADENCE}",
            rf"{_DAILY_CADENCE}\s+at\s+(?P<time>.+?)\s+(?:say|announce)\s+(?P<message>.+)",
        )
        for pattern in daily_message_patterns:
            if match := re.fullmatch(pattern, routed):
                return _daily_notification(match.group("message"), match.group("time"))
        if daily_web_action := _route_daily_web_action(routed, router):
            return daily_web_action
        recurring_patterns = (
            rf"remind me (?:every\s+|each\s+)?(?P<cadence>{_REMINDER_CADENCE}) "
            r"at (?P<time>.+?) to (?P<message>.+)",
            rf"(?:every|each) (?P<cadence>{_REMINDER_CADENCE}) "
            r"at (?P<time>.+?) remind me to (?P<message>.+)",
            rf"remind me to (?P<message>.+?) (?:every|each) "
            rf"(?P<cadence>{_REMINDER_CADENCE}) at (?P<time>.+)",
        )
        recurring_match = next(
            (
                match
                for pattern in recurring_patterns
                if (match := re.fullmatch(pattern, routed))
            ),
            None,
        )
        if recurring_match:
            local_time = _clock_time(recurring_match.group("time"))
            if local_time is None:
                return RouteResult(
                    None,
                    clarification="What time should the recurring reminder run, including AM or PM?",
                )
            cadence = recurring_match.group("cadence")
            if cadence in {"everyday", "daily"}:
                cadence = "day"
            elif cadence == "weekdays":
                cadence = "weekday"
            weekdays = {
                "monday": (0,),
                "tuesday": (1,),
                "wednesday": (2,),
                "thursday": (3,),
                "friday": (4,),
                "saturday": (5,),
                "sunday": (6,),
                "weekday": (0, 1, 2, 3, 4),
            }
            recurrence = {
                "frequency": "daily" if cadence == "day" else "weekly",
                "interval": 1,
                "local_time": local_time,
            }
            if cadence != "day":
                recurrence["weekdays"] = weekdays[cadence]
            return RouteResult(
                Intent(
                    name="core.schedule",
                    arguments={
                        "operation": "create",
                        "name": recurring_match.group("message"),
                        "message": recurring_match.group("message"),
                        "recurrence": recurrence,
                    },
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"remind me every (.+?) (minute|minutes|hour|hours) to (.+)",
            routed,
        ):
            try:
                interval = spoken_number(match.group(1))
            except ValueError:
                return RouteResult(None, clarification="How often should I remind you?")
            if not interval.is_integer() or interval < 1:
                return RouteResult(None, clarification="Use a whole number of minutes or hours.")
            return RouteResult(
                Intent(
                    name="core.schedule",
                    arguments={
                        "operation": "create",
                        "name": match.group(3),
                        "message": match.group(3),
                        "recurrence": {
                            "frequency": (
                                "minutes" if match.group(2).startswith("minute") else "hours"
                            ),
                            "interval": int(interval),
                        },
                    },
                    confidence=0.99,
                    source="rule",
                )
            )
        relative_patterns = (
            r"remind me in (?P<amount>.+?) (?P<unit>seconds?|minutes?|hours?|days?) "
            r"to (?P<title>.+)",
            r"remind me to (?P<title>.+?) in (?P<amount>.+?) "
            r"(?P<unit>seconds?|minutes?|hours?|days?)",
            r"in (?P<amount>.+?) (?P<unit>seconds?|minutes?|hours?|days?) "
            r"remind me to (?P<title>.+)",
            r"remind me after (?P<amount>.+?) (?P<unit>seconds?|minutes?|hours?|days?) "
            r"to (?P<title>.+)",
        )
        relative_match = next(
            (
                match
                for pattern in relative_patterns
                if (match := re.fullmatch(pattern, routed))
            ),
            None,
        )
        if relative_match:
            delay_seconds = _duration_seconds(
                relative_match.group("amount"),
                relative_match.group("unit"),
            )
            if delay_seconds is None:
                return RouteResult(None, clarification="How long from now should I remind you?")
            return RouteResult(
                Intent(
                    name="core.reminder",
                    arguments={
                        "title": relative_match.group("title"),
                        "delay_seconds": delay_seconds,
                    },
                    confidence=0.99,
                    source="rule",
                )
            )
        if routed in {
            "list schedules",
            "show schedules",
            "list reminders",
            "show reminders",
            "what are my reminders",
        }:
            return RouteResult(
                Intent(
                    name="core.schedule",
                    arguments={"operation": "list"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(pause|resume|delete) (?:schedule|reminder) (.+)", routed):
            return RouteResult(
                Intent(
                    name="core.schedule",
                    arguments={"operation": match.group(1), "schedule": match.group(2)},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"run (?:schedule|reminder) (.+) now", routed):
            return RouteResult(
                Intent(
                    name="core.schedule",
                    arguments={"operation": "run_now", "schedule": match.group(1)},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"snooze (?:schedule|reminder) (.+) for (.+?) (minute|minutes|hour|hours)",
            routed,
        ):
            try:
                amount = spoken_number(match.group(2))
            except ValueError:
                return RouteResult(None, clarification="How long should I snooze it?")
            multiplier = 60 if match.group(3).startswith("minute") else 3600
            return RouteResult(
                Intent(
                    name="core.schedule",
                    arguments={
                        "operation": "snooze",
                        "schedule": match.group(1),
                        "delay_seconds": amount * multiplier,
                    },
                    confidence=0.98,
                    source="rule",
                )
            )
        timer_patterns = (
            r"(?:set|start) (?:a )?timer (?:for|to) "
            r"(?P<amount>.+?) (?P<unit>seconds?|minutes?|hours?)",
            r"(?:set|start) (?:a )?(?P<amount>.+?) "
            r"(?P<unit>seconds?|minutes?|hours?) timer",
            r"(?:a )?timer for (?P<amount>.+?) (?P<unit>seconds?|minutes?|hours?)",
        )
        timer_match = next(
            (
                match
                for pattern in timer_patterns
                if (match := re.fullmatch(pattern, routed))
            ),
            None,
        )
        if timer_match:
            seconds = _duration_seconds(
                timer_match.group("amount"),
                timer_match.group("unit"),
            )
            if seconds is None or seconds > 86400:
                return RouteResult(None, clarification="How long should I set the timer for?")
            return RouteResult(
                Intent(
                    name="core.timer",
                    arguments={"seconds": seconds, "label": "Timer"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:what(?:'s| is)? (?:the )?time|what time is it|"
            r"tell me (?:the )?(?:current )?time|current time|time now)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.datetime",
                    arguments={"query": "time"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:what(?:'s| is)? (?:the |today'?s )?date|what day is it|"
            r"tell me (?:the |today'?s )?date|today'?s date|current date)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.datetime",
                    arguments={"query": "date"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(calculate|compute|evaluate|solve|what is|what's) (.+)",
            routed,
        ):
            expression = (
                match.group(2)
                .replace(" plus ", " + ")
                .replace(" minus ", " - ")
                .replace(" times ", " * ")
                .replace(" multiplied by ", " * ")
                .replace(" divided by ", " / ")
            )
            explicit_calculation = match.group(1) in {
                "calculate",
                "compute",
                "evaluate",
                "solve",
            }
            numeric_expression = bool(
                re.fullmatch(r"[\d\s.+\-*/()%]+", expression) and re.search(r"\d", expression)
            )
            if explicit_calculation or numeric_expression:
                return RouteResult(
                    Intent(
                        name="core.calculator",
                        arguments={"expression": expression},
                        confidence=0.96,
                        source="rule",
                    )
                )
        conversion_text = re.sub(
            r"\bdegrees?\s+(?=(?:celsius|fahrenheit)\b)",
            "",
            routed,
        )
        units = (
            r"kilometers?|miles?|meters?|feet|kilograms?|pounds?|"
            r"liters?|gallons?|celsius|fahrenheit"
        )
        conversion_patterns = (
            rf"convert (?P<value>.+?) (?P<from>{units}) (?:to|into|in) (?P<to>{units})",
            rf"how many (?P<to>{units}) (?:are(?: there)? )?in "
            rf"(?P<value>.+?) (?P<from>{units})",
        )
        conversion_match = next(
            (
                match
                for pattern in conversion_patterns
                if (match := re.fullmatch(pattern, conversion_text))
            ),
            None,
        )
        if conversion_match:
            try:
                value = spoken_number(conversion_match.group("value"))
            except ValueError:
                return RouteResult(None, clarification="What value should I convert?")
            return RouteResult(
                Intent(
                    name="core.unit_conversion",
                    arguments={
                        "value": value,
                        "from_unit": conversion_match.group("from"),
                        "to_unit": conversion_match.group("to"),
                    },
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:add|create|make|new) (?:a )?(?:new )?(?:task|to-?do) "
            r"(?:(?:called|named|to) )?(.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.tasks",
                    arguments={"operation": "add", "title": match.group(1)},
                    confidence=0.95,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"add (.+?) to (?:my |the )?(?:tasks|task list|to-?do list)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.tasks",
                    arguments={"operation": "add", "title": match.group(1)},
                    confidence=0.95,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:(?:complete|finish|delete|remove) (?:task|to-?do) (\d+)|"
            r"mark (?:task|to-?do) (\d+) (?:as )?(?:complete|completed|done))",
            routed,
        ):
            operation = (
                "delete"
                if routed.startswith(("delete ", "remove "))
                else "complete"
            )
            item_id = int(match.group(1) or match.group(2))
            return RouteResult(
                Intent(
                    name="core.tasks",
                    arguments={"operation": operation, "item_id": item_id},
                    confidence=0.98,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:list|show) (?:my |the )?(?:tasks|task list|to-?dos?|to-?do list)|"
            r"what are (?:my |the )?(?:tasks|to-?dos?)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.tasks",
                    arguments={"operation": "list"},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:save a note|make a note|note down) (.+)", routed):
            return RouteResult(
                Intent(
                    name="core.notes",
                    arguments={
                        "operation": "add",
                        "title": "Voice note",
                        "body": match.group(1),
                    },
                    confidence=0.9,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:(?:search|find) (?:my )?notes (?:for|about)|"
            r"look (?:in|through) (?:my )?notes for) (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.notes",
                    arguments={"operation": "search", "query": match.group(1)},
                    confidence=0.95,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:delete|remove) (?:my )?note (\d+)", routed):
            return RouteResult(
                Intent(
                    name="core.notes",
                    arguments={"operation": "delete", "item_id": int(match.group(1))},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:run|execute) macro (.+)", routed):
            return RouteResult(
                Intent(
                    name="core.macro",
                    arguments={"name": match.group(1)},
                    confidence=0.98,
                    source="rule",
                )
            )
        return None

    @staticmethod
    def _route_playbook_command(context: RouteContext) -> RouteResult | None:
        raw = context.text.strip()
        save_match = re.fullmatch(
            r"(?:(?:save|create|remember)\s+(?:a\s+)?playbook|"
            r"(?:save|remember)\s+(?:these\s+)?instructions\s+as\s+(?:a\s+)?playbook)"
            r"(?:\s+(?:called|named))?\s+"
            r"(?P<name>.+?)"
            r"(?:\s+(?:using|with)\s+skills?\s+(?P<skills>.+?))?"
            r"\s*(?::|\s+with\s+instructions)\s*(?P<instructions>.+)",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if save_match:
            raw_skills = save_match.group("skills")
            skill_ids = (
                tuple(
                    value.strip().casefold()
                    for value in re.split(r"\s*(?:,|\band\b)\s*", raw_skills)
                    if value.strip()
                )
                if raw_skills
                else None
            )
            arguments = {
                "operation": "save",
                "name": save_match.group("name").strip(),
                "instructions": save_match.group("instructions").strip(),
            }
            if skill_ids is not None:
                arguments["skill_ids"] = skill_ids
            return RouteResult(
                Intent(
                    name="core.playbook",
                    arguments=arguments,
                    confidence=0.99,
                    source="rule",
                )
            )

        routed = context.routed
        if routed in {
            "list playbooks",
            "show playbooks",
            "what are my playbooks",
            "what playbooks do i have",
        }:
            return RouteResult(
                Intent(
                    name="core.playbook",
                    arguments={"operation": "list"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(show|recall|inspect) (?:the )?playbook (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.playbook",
                    arguments={"operation": "show", "name": match.group(2)},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(enable|disable|delete) (?:the )?playbook (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.playbook",
                    arguments={"operation": match.group(1), "name": match.group(2)},
                    confidence=0.99,
                    source="rule",
                )
            )
        return None
