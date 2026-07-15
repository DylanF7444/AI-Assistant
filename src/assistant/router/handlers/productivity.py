from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.normalization import spoken_number
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


def _clock_time(value: str) -> str | None:
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", value.strip())
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3) == "pm" and hour != 12:
        hour += 12
    elif match.group(3) == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}:00"


class ProductivityHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        if match := re.fullmatch(
            r"remind me every (day|weekday|monday|tuesday|wednesday|thursday|friday|saturday|sunday) at (.+?) to (.+)",
            routed,
        ):
            local_time = _clock_time(match.group(2))
            if local_time is None:
                return RouteResult(
                    None,
                    clarification="What time should the recurring reminder run, including AM or PM?",
                )
            cadence = match.group(1)
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
                        "name": match.group(3),
                        "message": match.group(3),
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
        if match := re.fullmatch(
            r"remind me in (.+?) (second|seconds|minute|minutes|hour|hours|day|days) to (.+)",
            routed,
        ):
            try:
                amount = spoken_number(match.group(1))
            except ValueError:
                return RouteResult(None, clarification="How long from now should I remind you?")
            multiplier = {
                "second": 1,
                "seconds": 1,
                "minute": 60,
                "minutes": 60,
                "hour": 3600,
                "hours": 3600,
                "day": 86400,
                "days": 86400,
            }[match.group(2)]
            return RouteResult(
                Intent(
                    name="core.reminder",
                    arguments={
                        "title": match.group(3),
                        "delay_seconds": amount * multiplier,
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
        if match := re.fullmatch(
            r"(?:set|start) (?:a )?timer for (.+?) (second|seconds|minute|minutes|hour|hours)",
            routed,
        ):
            try:
                amount = spoken_number(match.group(1))
            except ValueError:
                return RouteResult(None, clarification="How long should I set the timer for?")
            multiplier = {
                "second": 1,
                "seconds": 1,
                "minute": 60,
                "minutes": 60,
                "hour": 3600,
                "hours": 3600,
            }[match.group(2)]
            return RouteResult(
                Intent(
                    name="core.timer",
                    arguments={"seconds": amount * multiplier, "label": "Timer"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if re.fullmatch(r"(?:what(?:'s| is)? )?(?:the )?time", routed):
            return RouteResult(
                Intent(
                    name="core.datetime",
                    arguments={"query": "time"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if re.fullmatch(r"(?:what(?:'s| is)? )?(?:the )?date", routed):
            return RouteResult(
                Intent(
                    name="core.datetime",
                    arguments={"query": "date"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(calculate|what is) (.+)", routed):
            expression = (
                match.group(2)
                .replace(" plus ", " + ")
                .replace(" minus ", " - ")
                .replace(" times ", " * ")
                .replace(" multiplied by ", " * ")
                .replace(" divided by ", " / ")
            )
            explicit_calculation = match.group(1) == "calculate"
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
        if match := re.fullmatch(
            r"convert (.+?) (kilometers?|miles?|meters?|feet|kilograms?|pounds?|liters?|gallons?|celsius|fahrenheit) to (kilometers?|miles?|meters?|feet|kilograms?|pounds?|liters?|gallons?|celsius|fahrenheit)",
            conversion_text,
        ):
            try:
                value = spoken_number(match.group(1))
            except ValueError:
                return RouteResult(None, clarification="What value should I convert?")
            return RouteResult(
                Intent(
                    name="core.unit_conversion",
                    arguments={
                        "value": value,
                        "from_unit": match.group(2),
                        "to_unit": match.group(3),
                    },
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:add|create) (?:a )?task (.+)", routed):
            return RouteResult(
                Intent(
                    name="core.tasks",
                    arguments={"operation": "add", "title": match.group(1)},
                    confidence=0.95,
                    source="rule",
                )
            )
        if routed in {"list tasks", "show tasks", "what are my tasks"}:
            return RouteResult(
                Intent(
                    name="core.tasks",
                    arguments={"operation": "list"},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:remember|save a note) (.+)", routed):
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
        if match := re.fullmatch(r"(?:search|find) (?:my )?notes for (.+)", routed):
            return RouteResult(
                Intent(
                    name="core.notes",
                    arguments={"operation": "search", "query": match.group(1)},
                    confidence=0.95,
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
