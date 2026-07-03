from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

from assistant.contracts import Intent
from assistant.router.normalization import command_text, normalize_text, spoken_number
from assistant.windows.process_names import executable_from_command_name


@dataclass(frozen=True)
class RouteResult:
    intent: Intent | None
    intents: tuple[Intent, ...] = ()
    needs_llm: bool = False
    clarification: str | None = None


class DeterministicRouter:
    def __init__(
        self,
        application_names: Callable[[], Iterable[str]] | Iterable[str] | None = None,
    ) -> None:
        self.application_names = application_names

    APP_REGISTRATION_HELP = (
        "Open the Applications tab, choose Register Application, select the app's "
        ".exe file, and give it the name you want to use. After that you can say "
        "\"open <name>\" and I will use that registered path."
    )

    CONTROL = {
        "cancel": "control.cancel",
        "stop": "control.cancel",
        "never mind": "control.cancel",
        "undo": "control.undo",
        "undo last command": "control.undo",
        "undo that": "control.undo",
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
    BROWSER_APPLICATIONS = {
        "browser",
        "brave",
        "brave browser",
        "chrome",
        "chromium",
        "google chrome",
        "edge",
        "microsoft edge",
        "firefox",
        "firefox developer edition",
        "firefox nightly",
        "mozilla firefox",
        "librewolf",
        "opera",
        "opera browser",
        "opera gx",
        "vivaldi",
        "vivaldi browser",
        "waterfox",
    }
    WEBSITE_APPLICATIONS = {
        "amazon": "https://www.amazon.com",
        "chat gpt": "https://chatgpt.com",
        "chatgpt": "https://chatgpt.com",
        "facebook": "https://www.facebook.com",
        "github": "https://github.com",
        "gmail": "https://mail.google.com",
        "google": "https://www.google.com",
        "google maps": "https://www.google.com/maps",
        "instagram": "https://www.instagram.com",
        "linkedin": "https://www.linkedin.com",
        "netflix": "https://www.netflix.com",
        "reddit": "https://www.reddit.com",
        "stack overflow": "https://stackoverflow.com",
        "twitch": "https://www.twitch.tv",
        "wikipedia": "https://www.wikipedia.org",
        "youtube": "https://www.youtube.com",
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
        if re.fullmatch(
            r"(?:list|show|what are|what're) (?:my |the )?"
            r"registered (?:apps|applications|programs)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.registered_applications",
                    arguments={},
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
        if match := re.fullmatch(r"(?:read|fetch) (https://\S+)", text.strip(), re.I):
            return RouteResult(
                Intent(
                    name="web.fetch",
                    arguments={"url": match.group(1), "max_characters": 12000},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"open (https://\S+)", text.strip(), re.I):
            return RouteResult(
                Intent(
                    name="windows.browser_navigate",
                    arguments={"url": match.group(1)},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"ask (chatgpt|codex|openai|claude) (?:for )?(.+)", routed):
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
        if match := re.fullmatch(
            r"(?:make|write|generate|create) (?:some )?(?:code|a script|an app|a program)(?: (?:to|for|that|which))? (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="cloud.ask",
                    arguments={
                        "provider": "codex",
                        "question": f"Write code {match.group(1)}",
                        "max_tokens": 4000,
                    },
                    confidence=0.96,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"code (?:up|me|for me) (.+)", routed):
            return RouteResult(
                Intent(
                    name="cloud.ask",
                    arguments={
                        "provider": "codex",
                        "question": f"Write code for {match.group(1)}",
                        "max_tokens": 4000,
                    },
                    confidence=0.94,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:search|find|show|list) installed (?:apps|applications|programs)(?: (?:for|matching|named) (.+))?",
            routed,
        ):
            arguments = {"operation": "search_installed_apps"}
            if match.group(1):
                arguments["query"] = match.group(1)
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments=arguments,
                    confidence=0.98,
                    source="rule",
                )
            )
        audio_target = r"(?:(?:my|the) )?(?:(?:system|device|computer) )?(?:volume|sound|audio)"
        if match := re.fullmatch(
            rf"(?:set|adjust) {audio_target}(?: to)? (.+?)(?: percent|%)?",
            routed,
        ):
            if (level := self._percent(match.group(1))) is not None:
                return RouteResult(
                    Intent(
                        name="windows.device_control",
                        arguments={"operation": "set_volume", "level": level},
                        confidence=0.98,
                        source="rule",
                    )
                )
        if match := re.fullmatch(
            rf"(increase|raise|decrease|lower|turn up|turn down) {audio_target}(?: by)? (.+?)(?: percent|%)?",
            routed,
        ):
            if (value := self._percent(match.group(2))) is not None:
                direction = -1 if match.group(1) in {"decrease", "lower", "turn down"} else 1
                return RouteResult(
                    Intent(
                        name="windows.device_control",
                        arguments={
                            "operation": "change_volume",
                            "delta": direction * value,
                        },
                        confidence=0.96,
                        source="rule",
                    )
                )
        if re.fullmatch(rf"mute {audio_target}", routed):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "mute_audio"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if re.fullmatch(rf"unmute {audio_target}", routed):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "unmute_audio"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:set|adjust) (?:screen )?brightness(?: to)? (.+?)(?: percent|%)?",
            routed,
        ):
            if (level := self._percent(match.group(1))) is not None:
                return RouteResult(
                    Intent(
                        name="windows.device_control",
                        arguments={"operation": "set_brightness", "level": level},
                        confidence=0.98,
                        source="rule",
                    )
                )
        if match := re.fullmatch(
            r"(increase|raise|decrease|lower|turn up|turn down) (?:screen )?brightness(?: by)? (.+?)(?: percent|%)?",
            routed,
        ):
            if (value := self._percent(match.group(2))) is not None:
                direction = -1 if match.group(1) in {"decrease", "lower", "turn down"} else 1
                return RouteResult(
                    Intent(
                        name="windows.device_control",
                        arguments={
                            "operation": "change_brightness",
                            "delta": direction * value,
                        },
                        confidence=0.96,
                        source="rule",
                    )
                )
        if match := re.fullmatch(
            r"connect (?:to )?(?:my |the )?(?:wi-?fi|wifi|wireless)(?: network)? (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={
                        "operation": "connect_wifi_network",
                        "network_name": match.group(1).strip(),
                    },
                    confidence=0.96,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"disconnect (?:from )?(?:my |the )?(?:wi-?fi|wifi|wireless)(?: network)?(?: (.+))?",
            routed,
        ):
            arguments = {"operation": "disconnect_wifi_network"}
            if match.group(1):
                arguments["network_name"] = match.group(1).strip()
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments=arguments,
                    confidence=0.96,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"connect (?:to )?(?:my |the )?bluetooth(?: device)? (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={
                        "operation": "connect_bluetooth_device",
                        "device_name": match.group(1).strip(),
                    },
                    confidence=0.96,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"disconnect (?:from )?(?:my |the )?bluetooth(?: device)? (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={
                        "operation": "disconnect_bluetooth_device",
                        "device_name": match.group(1).strip(),
                    },
                    confidence=0.96,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:(?:turn|toggle) (?:my |the )?(wi-?fi|wifi|wireless)(?: (on|off))?|turn (on|off) (?:my |the )?(wi-?fi|wifi|wireless)|(?:enable|disable) (?:my |the )?(wi-?fi|wifi|wireless))",
            routed,
        ):
            enabled = None
            if match.group(2):
                enabled = match.group(2) == "on"
            elif match.group(3):
                enabled = match.group(3) == "on"
            elif routed.startswith("enable "):
                enabled = True
            elif routed.startswith("disable "):
                enabled = False
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "toggle_wifi", "enabled": enabled},
                    confidence=0.97,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:(?:turn|toggle) (?:my |the )?bluetooth(?: (on|off))?|turn (on|off) (?:my |the )?bluetooth|(?:enable|disable) (?:my |the )?bluetooth)",
            routed,
        ):
            enabled = None
            if match.group(1):
                enabled = match.group(1) == "on"
            elif match.group(2):
                enabled = match.group(2) == "on"
            elif routed.startswith("enable "):
                enabled = True
            elif routed.startswith("disable "):
                enabled = False
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "toggle_bluetooth", "enabled": enabled},
                    confidence=0.97,
                    source="rule",
                )
            )
        if routed in {"lock device", "lock the device", "lock my device", "lock computer", "lock the computer"}:
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "lock_device"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if routed in {"restart device", "restart the device", "restart computer", "restart the computer", "reboot device", "reboot computer"}:
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "restart_device"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if routed in {"shut down device", "shutdown device", "shut down the device", "shutdown the device", "shut down computer", "shutdown computer"}:
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "shutdown_device"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:(?:show|check) (?:my |the )?battery(?: status| level)?|"
            r"what(?:'s|s| is) (?:my |the )?battery(?: status| level)?|"
            r"battery(?: status| level))",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "battery_status"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:show|check) (?:my |the )?storage status(?: for ([a-z]:?))?",
            routed,
        ):
            arguments = {"operation": "storage_status"}
            if match.group(1):
                arguments["drive"] = match.group(1)
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments=arguments,
                    confidence=0.99,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:show|check) (?:my |the )?(?:memory/cpu|cpu/memory|memory and cpu|cpu and memory|memory|cpu) usage",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "performance_status"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if routed in {
            "show connected devices",
            "list connected devices",
            "manage connected devices",
            "show devices",
            "list devices",
        }:
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "connected_devices"},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(enable|disable) connected device (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={
                        "operation": f"{match.group(1)}_connected_device",
                        "instance_id": match.group(2),
                    },
                    confidence=0.94,
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
            r"(?:move|place|put|switch) (.+?) (?:window )?(?:to|on) (?:my |the )?(?:(?:monitor|screen|display) )?(.+?)(?: (?:monitor|screen|display))?",
            routed,
        ):
            if monitor := self._monitor_index(match.group(2)):
                return RouteResult(
                    Intent(
                        name="windows.application_window",
                        arguments={
                            "operation": "move_to_monitor",
                            "process_name": executable_from_command_name(
                                match.group(1)
                            ),
                            "monitor": monitor,
                        },
                        confidence=0.93,
                        source="rule",
                    )
                )
        if match := re.fullmatch(
            r"(?:move|place|put) (.+?) (?:window )?to x ?(-?\d+) y ?(-?\d+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.application_window",
                    arguments={
                        "operation": "move",
                        "process_name": executable_from_command_name(match.group(1)),
                        "x": int(match.group(2)),
                        "y": int(match.group(3)),
                    },
                    confidence=0.91,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"resize (.+?) (?:window )?to (\d+) (?:by|x) (\d+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.application_window",
                    arguments={
                        "operation": "resize",
                        "process_name": executable_from_command_name(match.group(1)),
                        "width": int(match.group(2)),
                        "height": int(match.group(3)),
                    },
                    confidence=0.91,
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
        if re.fullmatch(
            r"(?:open|launch|start) (?:a |an |the )?(?:new |blank )?"
            r"(?:(?:notepad)(?: (?:file|document|doc|window|note|text file|txt file))?"
            r"|(?:(?:text|txt) file|document|doc|note) in notepad)"
            r"(?: (?:of|about|for|with|containing) .*)?",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.launcher",
                    arguments={"application": "notepad"},
                    confidence=0.94,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:open|launch|start) (?:a |an |the )?(?:new |blank )?(?:browser )?tab",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.launcher",
                    arguments={"application": "browser"},
                    confidence=0.94,
                    source="rule",
                )
            )
        if context_navigation := self._route_context_navigation(routed):
            return RouteResult(context_navigation)
        if match := re.fullmatch(
            r"(minimize|maximize)(?: (?:the |this |current |active |focused |foreground )*window)?",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.application_window",
                    arguments={"operation": f"{match.group(1)}_focused"},
                    confidence=0.96,
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
                        "process_name": executable_from_command_name(match.group(2)),
                    },
                    confidence=0.96,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(focus|minimize|maximize|close) (?:application )?([a-z0-9][a-z0-9_-]*)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.application_window",
                    arguments={
                        "operation": match.group(1),
                        "process_name": executable_from_command_name(match.group(2)),
                    },
                    confidence=0.94,
                    source="rule",
                )
            )
        if browser_intent := self._route_browser_tab_launch(routed):
            return RouteResult(browser_intent)
        if match := re.fullmatch(r"(?:open|launch|start) application (.+)", routed):
            raw_target = match.group(1)
            application, follow_up = self._split_launch_target(raw_target)
            intent = Intent(
                name="windows.launcher",
                arguments={"application": application},
                confidence=0.98,
                source="rule",
            )
            if follow_up_intent := self._route_launch_follow_up(application, follow_up):
                if self._is_direct_launcher_follow_up(intent, follow_up_intent):
                    return RouteResult(follow_up_intent)
                return RouteResult(intent, intents=(intent, follow_up_intent))
            if placement_intent := self._route_launch_placement(
                application, raw_target
            ):
                return RouteResult(intent, intents=(intent, placement_intent))
            return RouteResult(
                intent
            )
        if match := re.fullmatch(r"(?:open|launch|start) (.+)", routed):
            raw_target = match.group(1)
            application, follow_up = self._split_launch_target(raw_target)
            if not follow_up and (
                fallback_url := self._website_fallback_url(application)
            ):
                return RouteResult(
                    Intent(
                        name="windows.browser_navigate",
                        arguments={"url": fallback_url},
                        confidence=0.94,
                        source="rule",
                    )
                )
            arguments: dict[str, object] = {"application": application}
            intent = Intent(
                name="windows.launcher",
                arguments=arguments,
                confidence=0.9,
                source="rule",
            )
            if follow_up_intent := self._route_launch_follow_up(application, follow_up):
                if self._is_direct_launcher_follow_up(intent, follow_up_intent):
                    return RouteResult(follow_up_intent)
                return RouteResult(intent, intents=(intent, follow_up_intent))
            if placement_intent := self._route_launch_placement(
                application, raw_target
            ):
                return RouteResult(intent, intents=(intent, placement_intent))
            return RouteResult(
                intent
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
        if routed in {"toggle dark mode", "switch dark mode"}:
            return RouteResult(
                Intent(
                    name="windows.setting",
                    arguments={"operation": "toggle", "setting": "dark_mode"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"turn light mode (on|off)", routed):
            return RouteResult(
                Intent(
                    name="windows.setting",
                    arguments={
                        "operation": "set",
                        "setting": "dark_mode",
                        "enabled": match.group(1) == "off",
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

    def _registered_application_names(self) -> tuple[str, ...]:
        names = (
            self.application_names()
            if callable(self.application_names)
            else self.application_names
        )
        return tuple(
            sorted(
                {
                    name.strip().casefold()
                    for name in (names or ())
                    if isinstance(name, str) and name.strip()
                },
                key=len,
                reverse=True,
            )
        )

    @staticmethod
    def _percent(text: str) -> int | None:
        cleaned = re.sub(r"\b(?:percent|per cent)\b|%", "", text.strip().casefold()).strip()
        if match := re.fullmatch(r"\d{1,3}", cleaned):
            value = int(match.group(0))
        else:
            try:
                spoken = spoken_number(cleaned)
            except ValueError:
                return None
            if not spoken.is_integer():
                return None
            value = int(spoken)
        return value if 0 <= value <= 100 else None

    @staticmethod
    def _route_context_navigation(routed: str) -> Intent | None:
        patterns = (
            (
                "new_tab",
                (
                    r"(?:open|launch|start|create) (?:a |an |the )?(?:new |blank )?(?:browser )?tab",
                    r"(?:new|blank) (?:browser )?tab",
                ),
            ),
            (
                "new_window",
                (
                    r"(?:open|launch|start|create) (?:a |an |the )?(?:new |blank )?(?:browser )?window",
                    r"(?:new|blank) (?:browser )?window",
                ),
            ),
            (
                "close_tab",
                (
                    r"(?:close|shut) (?:this |current |active |the )?(?:browser )?tab",
                    r"close tab",
                ),
            ),
            (
                "reopen_closed_tab",
                (
                    r"(?:reopen|restore) (?:the |my |that )?(?:last |recently )?closed (?:browser )?tab",
                ),
            ),
            (
                "next_tab",
                (
                    r"(?:go to |switch to )?next (?:browser )?tab",
                    r"(?:go|switch) (?:to )?the next (?:browser )?tab",
                ),
            ),
            (
                "previous_tab",
                (
                    r"(?:go to |switch to )?(?:previous|prev|last) (?:browser )?tab",
                    r"(?:go|switch) (?:to )?the (?:previous|prev|last) (?:browser )?tab",
                ),
            ),
            (
                "focus_address_bar",
                (
                    r"(?:focus|select|highlight|open|go to) (?:the )?(?:address bar|url bar|omnibox)",
                ),
            ),
            (
                "back",
                (
                    r"(?:go|navigate) back(?: (?:a|one) page)?",
                    r"back(?: (?:a|one) page)?",
                ),
            ),
            (
                "forward",
                (
                    r"(?:go|navigate) forward(?: (?:a|one) page)?",
                    r"forward(?: (?:a|one) page)?",
                ),
            ),
            (
                "reload",
                (
                    r"(?:reload|refresh)(?: (?:this |the |current )?(?:page|tab|browser))?",
                ),
            ),
        )
        for operation, operation_patterns in patterns:
            if any(re.fullmatch(pattern, routed) for pattern in operation_patterns):
                return Intent(
                    name="windows.context_navigation",
                    arguments={"target": "browser", "operation": operation},
                    confidence=0.93,
                    source="rule",
                )
        return None

    @staticmethod
    def _route_browser_tab_launch(routed: str) -> Intent | None:
        browser_names = "|".join(
            re.escape(name)
            for name in sorted(
                DeterministicRouter.BROWSER_APPLICATIONS, key=len, reverse=True
            )
        )
        website_first_intent = (
            DeterministicRouter._route_website_first_private_browser_launch(
                routed, browser_names
            )
        )
        if website_first_intent:
            return website_first_intent
        website_match = re.fullmatch(
            rf"(?:open|launch|start)\s+"
            rf"(?:a |an |the )?"
            rf"(?:new\s+)?"
            rf"(?P<private_before>private|incognito|inprivate)\s+"
            rf"(?P<application>{browser_names})\s+"
            rf"(?:(?:browser)\s+)?"
            rf"(?:tab|window)\s+"
            rf"(?:with|to|for|at)\s+"
            rf"(?P<website>.+)",
            routed,
        )
        if not website_match:
            website_match = re.fullmatch(
                rf"(?:open|launch|start)\s+"
                rf"(?:a |an |the )?"
                rf"(?:new\s+)?"
                rf"(?P<application>{browser_names})\s+"
                rf"(?:(?P<private_after>private|incognito|inprivate)\s+)?"
                rf"(?:(?:browser)\s+)?"
                rf"(?:tab|window)\s+"
                rf"(?:with|to|for|at)\s+"
                rf"(?P<website>.+)",
                routed,
            )
        if not website_match:
            website_match = re.fullmatch(
                rf"(?:open|launch|start)\s+"
                rf"(?:a |an |the )?"
                rf"(?:new\s+)?"
                rf"(?P<private_before>private|incognito|inprivate)\s+"
                rf"(?:(?:browser)\s+)?"
                rf"(?:tab|window)\s+"
                rf"(?:in|on|with|using)\s+"
                rf"(?P<application>{browser_names})\s+"
                rf"(?:with|to|for|at)\s+"
                rf"(?P<website>.+)",
                routed,
            )
        if not website_match:
            website_match = re.fullmatch(
                r"(?:open|launch|start)\s+"
                r"(?:a |an |the )?"
                r"(?:new\s+)?"
                r"(?P<private_before>private|incognito|inprivate)\s+"
                r"(?:(?:browser)\s+)?"
                r"(?:tab|window)\s+"
                r"(?:with|to|for|at)\s+"
                r"(?P<website>.+)",
                routed,
            )
        if not website_match:
            website_match = re.fullmatch(
                r"(?:open|launch|start)\s+"
                r"(?:a |an |the )?"
                r"(?:new\s+)?"
                r"(?:(?:browser)\s+)?"
                r"(?:tab|window)\s+"
                r"(?:with|to|for|at)\s+"
                r"(?P<website>.+)",
                routed,
            )
        if website_match and (
            url := DeterministicRouter._website_url(website_match.group("website"))
        ):
            application = (
                website_match.groupdict().get("application") or "browser"
            ).strip()
            private = bool(website_match.groupdict().get("private_before")) or bool(
                (website_match.groupdict().get("private_after") or "").strip()
            )
            arguments: dict[str, object] = {
                "application": application,
                "url": url,
            }
            if private:
                arguments["private"] = True
            return Intent(
                name="windows.launcher",
                arguments=arguments,
                confidence=0.95,
                source="rule",
            )
        match = re.fullmatch(
            rf"(?:open|launch|start)\s+"
            rf"(?:a |an |the )?"
            rf"(?:new\s+)?"
            rf"(?P<private_before>private|incognito|inprivate)?\s*"
            rf"(?P<application>{browser_names})\s+"
            rf"(?:(?:browser)\s+)?"
            rf"(?:(?P<private_after>private|incognito|inprivate)\s+)?"
            rf"(?:tab|window)",
            routed,
        )
        if not match:
            match = re.fullmatch(
                rf"(?:open|launch|start)\s+"
                rf"(?:a |an |the )?"
                rf"(?:new\s+)?"
                rf"(?P<private_before>private|incognito|inprivate)\s+"
                rf"(?P<application>{browser_names})",
                routed,
            )
        if not match:
            match = re.fullmatch(
                rf"(?:open|launch|start)\s+"
                rf"(?:a |an |the )?"
                rf"(?:new\s+)?"
                rf"(?P<application>{browser_names})\s+"
                rf"(?P<private_after>private|incognito|inprivate)",
                routed,
            )
        if not match:
            match = re.fullmatch(
                rf"(?:open|launch|start)\s+"
                rf"(?:a |an |the )?"
                rf"(?:new\s+)?"
                rf"(?P<private_before>private|incognito|inprivate)\s+"
                rf"(?:(?:browser)\s+)?"
                rf"(?:tab|window)\s+"
                rf"(?:in|on|with|using)\s+"
                rf"(?P<application>{browser_names})",
                routed,
            )
        if not match:
            match = re.fullmatch(
                r"(?:open|launch|start)\s+"
                r"(?:a |an |the )?"
                r"(?:new\s+)?"
                r"(?P<private_before>private|incognito|inprivate)\s+"
                r"(?:(?:browser)\s+)?"
                r"(?:tab|window)",
                routed,
            )
        if not match:
            return None
        application = (match.groupdict().get("application") or "browser").strip()
        private = bool(match.groupdict().get("private_before")) or bool(
            (match.groupdict().get("private_after") or "").strip()
        )
        if application == "browser" and not private:
            operation = "new_window" if routed.endswith("window") else "new_tab"
            return Intent(
                name="windows.context_navigation",
                arguments={"target": "browser", "operation": operation},
                confidence=0.94,
                source="rule",
            )
        arguments: dict[str, object] = {
            "application": application,
        }
        if private:
            arguments["private"] = True
        return Intent(
            name="windows.launcher",
            arguments=arguments,
            confidence=0.94,
            source="rule",
        )

    @staticmethod
    def _route_website_first_private_browser_launch(
        routed: str, browser_names: str
    ) -> Intent | None:
        patterns = (
            rf"(?:open|launch|start)\s+"
            rf"(?P<website>.+?)\s+"
            rf"(?:with|in|on|using|for)\s+"
            rf"(?:a |an |the )?"
            rf"(?P<private>private|incognito|inprivate)\s+"
            rf"(?:(?:browser)\s+)?"
            rf"(?:tab|window)\s+"
            rf"(?:(?:open|opened)\s+)?"
            rf"(?:for|in|on|with|using)\s+"
            rf"(?P<application>{browser_names})",
            rf"(?:open|launch|start)\s+"
            rf"(?P<website>.+?)\s+"
            rf"(?:with|in|on|using|for)\s+"
            rf"(?:a |an |the )?"
            rf"(?P<private>private|incognito|inprivate)\s+"
            rf"(?P<application>{browser_names})\s+"
            rf"(?:(?:browser)\s+)?"
            rf"(?:tab|window)",
            rf"(?:open|launch|start)\s+"
            rf"(?P<website>.+?)\s+"
            rf"(?:with|in|on|using|for)\s+"
            rf"(?P<application>{browser_names})\s+"
            rf"(?P<private>private|incognito|inprivate)\s+"
            rf"(?:(?:browser)\s+)?"
            rf"(?:tab|window)",
        )
        for pattern in patterns:
            if not (match := re.fullmatch(pattern, routed)):
                continue
            if not (url := DeterministicRouter._website_url(match.group("website"))):
                continue
            return Intent(
                name="windows.launcher",
                arguments={
                    "application": match.group("application").strip(),
                    "private": True,
                    "url": url,
                },
                confidence=0.95,
                source="rule",
            )
        return None

    def _split_launch_target(self, target: str) -> tuple[str, str | None]:
        target = target.strip()
        if website_browser := self._split_website_browser_target(target):
            return website_browser
        target = self._strip_launch_placement_suffix(target)
        folded = target.casefold()
        for name in self._registered_application_names():
            if folded == name:
                return target[: len(name)].strip(), None
            if folded.startswith(f"{name} "):
                remainder = target[len(name) :].strip()
                if self._looks_like_follow_up(remainder):
                    return target[: len(name)].strip(), remainder
        for separator in (" and then ", " then ", " and "):
            if separator not in folded:
                continue
            index = folded.find(separator)
            application = target[:index].strip()
            remainder = target[index + len(separator) :].strip()
            if application and self._looks_like_follow_up(remainder):
                return application, remainder
        if " " in target:
            parts = target.split()
            for size in (2, 1):
                application = " ".join(parts[:size])
                remainder = " ".join(parts[size:])
                if (
                    application.casefold() in self.BROWSER_APPLICATIONS
                    and remainder
                    and self._looks_like_follow_up(remainder)
                ):
                    return application, remainder
        return target, None

    def _split_website_browser_target(self, target: str) -> tuple[str, str] | None:
        folded = target.casefold()
        names = self._registered_application_names() + tuple(
            sorted(self.BROWSER_APPLICATIONS, key=len, reverse=True)
        )
        for name in names:
            for preposition in (" on ", " in ", " with ", " using "):
                suffix = f"{preposition}{name}"
                if not folded.endswith(suffix):
                    continue
                website = target[: -len(suffix)].strip()
                if website and self._website_url(website):
                    return name, website
        return None

    @staticmethod
    def _monitor_index(text: str) -> int | None:
        cleaned = re.sub(
            r"\b(?:my|the|monitor|screen|display)\b",
            "",
            text.strip().casefold(),
        ).strip()
        ordinal_words = {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
            "fifth": 5,
            "sixth": 6,
            "seventh": 7,
            "eighth": 8,
            "ninth": 9,
            "tenth": 10,
        }
        if cleaned in ordinal_words:
            return ordinal_words[cleaned]
        if match := re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", cleaned):
            return int(match.group(1))
        try:
            value = spoken_number(cleaned)
        except ValueError:
            return None
        return int(value) if value >= 1 and value.is_integer() else None

    @staticmethod
    def _route_launch_placement(application: str, target: str) -> Intent | None:
        monitor = DeterministicRouter._placement_monitor(target)
        if monitor is None:
            return None
        return Intent(
            name="windows.application_window",
            arguments={
                "operation": "move_to_monitor",
                "process_name": executable_from_command_name(application),
                "monitor": monitor,
            },
            confidence=0.88,
            source="rule",
        )

    @staticmethod
    def _placement_monitor(target: str) -> int | None:
        if match := re.search(
            r"\s+on\s+(?:(?:my|the)\s+)?(?:(?:monitor|screen|display)\s+(.+?)|"
            r"(.+?)\s+(?:monitor|screen|display))$",
            target,
            flags=re.I,
        ):
            return DeterministicRouter._monitor_index(
                (match.group(1) or match.group(2) or "").strip()
            )
        return None

    @staticmethod
    def _strip_launch_placement_suffix(target: str) -> str:
        monitor = (
            r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
            r"one|two|three|four|five|six|seven|eight|nine|ten|"
            r"\d+(?:st|nd|rd|th)?)"
        )
        return re.sub(
            rf"\s+on\s+(?:(?:my|the)\s+)?(?:(?:monitor|screen|display)\s+{monitor}|"
            rf"{monitor}\s+(?:monitor|screen|display))$",
            "",
            target,
            flags=re.I,
        ).strip()

    @staticmethod
    def _looks_like_follow_up(text: str) -> bool:
        text = re.sub(r"^(?:and then|and|then)\s+", "", text.strip().casefold())
        return bool(DeterministicRouter._website_url(text)) or bool(
            re.match(
                r"(?:search|find|look up|google|bing|duckduckgo|open|go to|navigate to|"
                r"read|fetch|ask|type|click|focus|minimize|maximize|close)\b",
                text,
            )
        )

    @staticmethod
    def _is_direct_launcher_follow_up(intent: Intent, follow_up: Intent) -> bool:
        return (
            follow_up.name == "windows.launcher"
            and intent.arguments.get("application")
            == follow_up.arguments.get("application")
            and bool(follow_up.arguments.get("url"))
        )

    @staticmethod
    def _route_launch_follow_up(application: str, text: str | None) -> Intent | None:
        if not text:
            return None
        routed = command_text(text)
        routed = re.sub(r"^(?:and then|and|then)\s+", "", routed)
        if match := re.fullmatch(
            r"(?:search|find|look up|google|bing|duckduckgo)(?: (?:the )?web)?(?: for)? (.+)",
            routed,
        ):
            return Intent(
                name="windows.launcher",
                arguments={
                    "application": application,
                    "url": "https://search.brave.com/search?"
                    + urlencode({"q": match.group(1)}),
                },
                confidence=0.94,
                source="rule",
            )
        if match := re.fullmatch(
            r"(?:read|fetch|open|go to|navigate to) (.+)",
            routed,
        ):
            if url := DeterministicRouter._website_url(match.group(1)):
                return Intent(
                    name="windows.launcher",
                    arguments={"application": application, "url": url},
                    confidence=0.94,
                    source="rule",
                )
        if url := DeterministicRouter._website_url(routed):
            return Intent(
                name="windows.launcher",
                arguments={"application": application, "url": url},
                confidence=0.94,
                source="rule",
            )
        return None

    @staticmethod
    def _website_url(target: str) -> str | None:
        target = DeterministicRouter._normalize_website_target(target)
        if fallback_url := DeterministicRouter._website_fallback_url(target):
            return fallback_url
        if not target or re.search(r"\s", target):
            return None
        if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I):
            if not target.lower().startswith("https://"):
                return None
            parsed = urlsplit(target)
        else:
            parsed = urlsplit(f"https://{target}")
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or "." not in parsed.netloc
        ):
            return None
        hostname = parsed.hostname or ""
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", hostname, re.I):
            return None
        return urlunsplit(parsed)

    @staticmethod
    def _normalize_website_target(target: str) -> str:
        target = target.strip().rstrip(".,;")
        replacements = (
            (r"\s+(?:dot|period)\s+", "."),
            (r"\s+(?:forward\s+slash|slash)\s+", "/"),
            (r"\s+(?:question\s+mark)\s+", "?"),
            (r"\s+(?:ampersand|and)\s+", "&"),
            (r"\s+(?:equals|equal)\s+", "="),
        )
        for pattern, replacement in replacements:
            target = re.sub(pattern, replacement, target, flags=re.I)
        return target

    @staticmethod
    def _website_fallback_url(target: str) -> str | None:
        target = re.sub(
            r"^(?:a|an|the)\s+",
            "",
            target.strip().casefold(),
        )
        if target in DeterministicRouter.WEBSITE_APPLICATIONS:
            return DeterministicRouter.WEBSITE_APPLICATIONS[target]
        return DeterministicRouter._domain_url(target)

    @staticmethod
    def _domain_url(target: str) -> str | None:
        if not target or re.search(r"\s", target):
            return None
        if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I):
            return None
        parsed = urlsplit(f"https://{target}")
        hostname = parsed.hostname or ""
        if not parsed.netloc or "." not in parsed.netloc:
            return None
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", hostname, re.I):
            return None
        return urlunsplit(parsed)
