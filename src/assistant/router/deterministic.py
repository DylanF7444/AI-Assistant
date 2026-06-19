from __future__ import annotations

import re
from dataclasses import dataclass

from assistant.contracts import Intent
from assistant.router.normalization import command_text, normalize_text, spoken_number


@dataclass(frozen=True)
class RouteResult:
    intent: Intent | None
    needs_llm: bool = False
    clarification: str | None = None


class DeterministicRouter:
    APP_REGISTRATION_HELP = (
        "Open the Applications tab, choose Register Application, select the app's "
        ".exe file, and give it the name you want to use. After that you can say "
        "\"open <name>\" and I will use that registered path."
    )

    CONTROL = {
        "cancel": "control.cancel",
        "stop": "control.cancel",
        "never mind": "control.cancel",
        "mute": "control.mute",
        "yes": "control.confirm",
        "confirm": "control.confirm",
        "no": "control.deny",
        "deny": "control.deny",
        "help": "core.help",
        "forget this session": "control.forget",
        "forget this conversation": "control.forget",
        "forget everything from this session": "control.forget",
    }

    def route(self, text: str) -> RouteResult:
        normalized = normalize_text(text)
        routed = command_text(text)
        if routed in self.CONTROL:
            return RouteResult(
                Intent(
                    name=self.CONTROL[routed],
                    confidence=1,
                    source="deterministic",
                )
            )
        if match := re.fullmatch(r"correct that to (.+)", routed):
            return RouteResult(
                Intent(
                    name="control.correct",
                    arguments={"text": match.group(1)},
                    confidence=1,
                    source="deterministic",
                )
            )
        if match := re.fullmatch(r"save that (.+)", routed):
            return RouteResult(
                Intent(
                    name="core.notes",
                    arguments={
                        "operation": "add",
                        "title": "Saved information",
                        "body": match.group(1),
                    },
                    confidence=0.98,
                    source="rule",
                )
            )
        if routed in {
            "run diagnostics",
            "diagnostics",
            "check assistant health",
            "system status",
        }:
            return RouteResult(
                Intent(
                    name="core.diagnostic",
                    arguments={"include_skills": True},
                    confidence=1,
                    source="deterministic",
                )
            )
        if re.fullmatch(
            r"(?:how do i |how can i |where do i |where can i )?"
            r"(?:register|add) (?:an? |the )?(?:application|app)"
            r"(?: so you can open it(?: for me)?| for launching| to open)?",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="conversation.answer",
                    arguments={"text": self.APP_REGISTRATION_HELP},
                    confidence=0.99,
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
                return RouteResult(
                    None, clarification="How long should I set the timer for?"
                )
            multiplier = {"second": 1, "seconds": 1, "minute": 60, "minutes": 60, "hour": 3600, "hours": 3600}[match.group(2)]
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
                re.fullmatch(r"[\d\s.+\-*/()%]+", expression)
                and re.search(r"\d", expression)
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
        if match := re.fullmatch(
            r"convert (.+?) (kilometers?|miles?|meters?|feet|kilograms?|pounds?|liters?|gallons?|celsius|fahrenheit) to (kilometers?|miles?|meters?|feet|kilograms?|pounds?|liters?|gallons?|celsius|fahrenheit)",
            routed,
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
        if match := re.fullmatch(r"(?:search the web|web search) for (.+)", routed):
            return RouteResult(
                Intent(
                    name="web.search",
                    arguments={"query": match.group(1), "count": 5},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:read|fetch|open) (https://\S+)", text.strip(), re.I):
            return RouteResult(
                Intent(
                    name="web.fetch",
                    arguments={"url": match.group(1), "max_characters": 12000},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"ask (chatgpt|codex|openai|claude) (.+)", routed):
            provider = {
                "claude": "anthropic",
                "codex": "codex",
            }.get(match.group(1), "openai")
            arguments = {
                "provider": provider,
                "question": match.group(2),
                "max_tokens": 1500,
            }
            return RouteResult(
                Intent(
                    name="cloud.ask",
                    arguments=arguments,
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:play|pause|play pause)(?: the)? music", routed):
            return RouteResult(
                Intent(
                    name="windows.media",
                    arguments={"action": "play_pause"},
                    confidence=0.95,
                    source="rule",
                )
            )
        if routed in {"mute volume", "mute the volume", "mute sound"}:
            return RouteResult(
                Intent(
                    name="windows.media",
                    arguments={"action": "volume_mute"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if routed in {"volume up", "turn volume up", "increase volume"}:
            return RouteResult(
                Intent(
                    name="windows.media",
                    arguments={"action": "volume_up"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if routed in {"volume down", "turn volume down", "decrease volume"}:
            return RouteResult(
                Intent(
                    name="windows.media",
                    arguments={"action": "volume_down"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(copy|move|rename) (.+?) (?:to|as) (.+)", routed
        ):
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={
                        "operation": match.group(1),
                        "source": match.group(2),
                        "destination": match.group(3),
                    },
                    confidence=0.92,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:delete|recycle) (?:file )?(.+)", routed):
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={"operation": "delete", "source": match.group(1)},
                    confidence=0.9,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"open file (.+)", routed):
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={"operation": "open", "source": match.group(1)},
                    confidence=0.94,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(focus|minimize|maximize|close) (?:application )?([^ ]+\.exe)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.application_window",
                    arguments={
                        "operation": match.group(1),
                        "process_name": match.group(2),
                    },
                    confidence=0.96,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:open|launch|start) application (.+)", routed):
            return RouteResult(
                Intent(
                    name="windows.launcher",
                    arguments={"application": match.group(1)},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:open|launch|start) ([a-z0-9][a-z0-9 ._-]*)", routed):
            return RouteResult(
                Intent(
                    name="windows.launcher",
                    arguments={"application": match.group(1).strip()},
                    confidence=0.9,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:terminate|end) process (\d+)", routed):
            return RouteResult(
                Intent(
                    name="windows.process",
                    arguments={"operation": "terminate", "pid": int(match.group(1))},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:list|show) processes(?: matching (.+))?", routed):
            return RouteResult(
                Intent(
                    name="windows.process",
                    arguments={"operation": "list", "name_filter": match.group(1)},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:show|create) notification (.+?): (.+)", routed):
            return RouteResult(
                Intent(
                    name="windows.notification",
                    arguments={"title": match.group(1), "message": match.group(2)},
                    confidence=0.95,
                    source="rule",
                )
            )
        if routed in {"is dark mode on", "check dark mode"}:
            return RouteResult(
                Intent(
                    name="windows.setting",
                    arguments={"operation": "read", "setting": "dark_mode"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"turn dark mode (on|off)", routed):
            return RouteResult(
                Intent(
                    name="windows.setting",
                    arguments={
                        "operation": "set",
                        "setting": "dark_mode",
                        "enabled": match.group(1) == "on",
                    },
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"(?:find|search for) (.+) in (.+)", routed):
            return RouteResult(
                Intent(
                    name="windows.file_search",
                    arguments={"query": match.group(1), "root": match.group(2), "limit": 20},
                    confidence=0.88,
                    source="rule",
                    ambiguous=True,
                    ambiguity_reasons=("filesystem path must be resolved and approved",),
                )
            )
        question = normalized.endswith("?") or normalized.startswith(
            ("who ", "what ", "when ", "where ", "why ", "how ")
        )
        return RouteResult(
            Intent(
                name="conversation.question" if question else "planner.request",
                arguments={"text": text},
                confidence=0.5,
                source="classifier",
                ambiguous=True,
                ambiguity_reasons=("No deterministic rule matched",),
            ),
            needs_llm=True,
        )
