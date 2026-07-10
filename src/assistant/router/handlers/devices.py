from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class DeviceStatusHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
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
        if re.fullmatch(
            r"(?:show|list) (?:the )?file (?:access )?(?:policy|lists|blacklist|whitelist|blacklists|whitelists)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.file_access_policy",
                    arguments={"operation": "list"},
                    confidence=0.96,
                    source="rule",
                )
            )
        if policy_intent := router._route_file_access_policy(routed):
            return RouteResult(policy_intent)
        audio_target = r"(?:(?:my|the) )?(?:(?:system|device|computer) )?(?:volume|sound|audio)"
        if (
            match := re.fullmatch(
                rf"(?:set|adjust) {audio_target}(?: to)? (.+?)(?: percent|%)?",
                routed,
            )
        ) and (level := router._percent(match.group(1))) is not None:
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "set_volume", "level": level},
                    confidence=0.98,
                    source="rule",
                )
            )
        if (
            match := re.fullmatch(
                rf"(increase|raise|decrease|lower|turn up|turn down) {audio_target}(?: by)? (.+?)(?: percent|%)?",
                routed,
            )
        ) and (value := router._percent(match.group(2))) is not None:
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
        if (
            match := re.fullmatch(
                r"(?:set|adjust) (?:screen )?brightness(?: to)? (.+?)(?: percent|%)?",
                routed,
            )
        ) and (level := router._percent(match.group(1))) is not None:
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "set_brightness", "level": level},
                    confidence=0.98,
                    source="rule",
                )
            )
        if (
            match := re.fullmatch(
                r"(increase|raise|decrease|lower|turn up|turn down) (?:screen )?brightness(?: by)? (.+?)(?: percent|%)?",
                routed,
            )
        ) and (value := router._percent(match.group(2))) is not None:
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
        return None
