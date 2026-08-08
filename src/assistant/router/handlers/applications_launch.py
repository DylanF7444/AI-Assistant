from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult
from assistant.windows.process_names import executable_from_command_name

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class ApplicationHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
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
        if context_navigation := router._route_context_navigation(routed):
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
            r"(focus|minimize|maximize|close) "
            r"(?P<article>(?:the|my) )?(?:application |app )?"
            r"(?P<application>.+)",
            routed,
        ):
            application = re.sub(
                r"\s+window$",
                "",
                match.group("application").strip(),
            )
            existing_single_word_form = (
                match.group("article") is None
                and " " not in application
                and not match.group("application").endswith(" window")
            )
            if router._is_known_application_reference(application) or existing_single_word_form:
                return RouteResult(
                    Intent(
                        name="windows.application_window",
                        arguments={
                            "operation": match.group(1),
                            "process_name": executable_from_command_name(application),
                        },
                        confidence=0.96,
                        source="rule",
                    )
                )
        if browser_intent := router._route_browser_tab_launch(routed):
            return RouteResult(browser_intent)
        if match := re.fullmatch(r"run (?:application |app )?(.+)", routed):
            application = match.group(1).strip()
            if router._is_known_application_reference(application):
                return RouteResult(
                    Intent(
                        name="windows.launcher",
                        arguments={"application": application},
                        confidence=0.95,
                        source="rule",
                    )
                )
        if match := re.fullmatch(r"(?:open|launch|start) application (.+)", routed):
            raw_target = match.group(1)
            application, follow_up = router._split_launch_target(raw_target)
            intent = Intent(
                name="windows.launcher",
                arguments={"application": application},
                confidence=0.98,
                source="rule",
            )
            if follow_up_intent := router._route_launch_follow_up(application, follow_up):
                if router._is_direct_launcher_follow_up(intent, follow_up_intent):
                    return RouteResult(follow_up_intent)
                return RouteResult(intent, intents=(intent, follow_up_intent))
            if placement_intent := router._route_launch_placement(application, raw_target):
                return RouteResult(intent, intents=(intent, placement_intent))
            return RouteResult(intent)
        if match := re.fullmatch(r"(?:open|launch|start) (.+)", routed):
            raw_target = match.group(1)
            application, follow_up = router._split_launch_target(raw_target)
            application = _normalize_browser_reference(application)
            if not follow_up and (fallback_url := router._website_fallback_url(application)):
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
            if follow_up_intent := router._route_launch_follow_up(application, follow_up):
                if router._is_direct_launcher_follow_up(intent, follow_up_intent):
                    return RouteResult(follow_up_intent)
                return RouteResult(intent, intents=(intent, follow_up_intent))
            if placement_intent := router._route_launch_placement(application, raw_target):
                return RouteResult(intent, intents=(intent, placement_intent))
            return RouteResult(intent)
        if match := re.fullmatch(r"(?:terminate|end|kill|stop) process (\d+)", routed):
            return RouteResult(
                Intent(
                    name="windows.process",
                    arguments={"operation": "terminate", "pid": int(match.group(1))},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:list|show)(?: (?:the|all))? (?:running )?processes"
            r"(?: (?:matching|named|for) (.+))?",
            routed,
        ):
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
        if match := re.fullmatch(
            r"(?:notify me|(?:show|create)(?: me)? (?:a )?notification)"
            r"(?: (?:saying|with message))?\s*:\s*(.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.notification",
                    arguments={"title": "Assistant", "message": match.group(1)},
                    confidence=0.94,
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
        if match := re.fullmatch(
            r"(?:turn dark mode (on|off)|turn (on|off) dark mode|"
            r"(enable|disable) dark mode)",
            routed,
        ):
            state = match.group(1) or match.group(2)
            if match.group(3):
                state = "on" if match.group(3) == "enable" else "off"
            return RouteResult(
                Intent(
                    name="windows.setting",
                    arguments={
                        "operation": "set",
                        "setting": "dark_mode",
                        "enabled": state == "on",
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
        if match := re.fullmatch(
            r"(?:turn light mode (on|off)|turn (on|off) light mode|"
            r"(enable|disable|use) light mode)",
            routed,
        ):
            state = match.group(1) or match.group(2)
            if match.group(3):
                state = "off" if match.group(3) == "disable" else "on"
            return RouteResult(
                Intent(
                    name="windows.setting",
                    arguments={
                        "operation": "set",
                        "setting": "dark_mode",
                        "enabled": state == "off",
                    },
                    confidence=0.99,
                    source="rule",
                )
            )
        return None


def _normalize_browser_reference(application: str) -> str:
    """Canonicalize relational browser wording without naming a specific browser."""
    value = application.strip()
    if re.fullmatch(
        r"(?:(?:my|the)\s+)?(?:(?:default|preferred|usual|regular)\s+)?browser",
        value,
        flags=re.IGNORECASE,
    ):
        return "browser"
    return value
