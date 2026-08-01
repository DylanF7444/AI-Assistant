from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class DeviceControlHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
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
        if re.fullmatch(
            r"lock (?:my |the )?(?:device|computer|pc|system)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "lock_device"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:restart|reboot) (?:my |the )?(?:device|computer|pc|system)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "restart_device"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:shut down|shutdown|turn off|power off) "
            r"(?:my |the )?(?:device|computer|pc|system)",
            routed,
        ):
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
            r"battery(?: status| level)|"
            r"how much battery (?:do i have|is)(?: left| remaining)?)",
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
            r"(?:(?:show|check|report)(?: me)? (?:my |the )?"
            r"(?:storage(?: status| space| usage)?|disk space|free space)|"
            r"how much (?:storage|disk space)(?: do i have| is)? "
            r"(?:left|available|free|remaining))"
            r"(?: (?:for|on) (?:drive )?([a-z]:?))?",
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
        performance_metric = r"(?:memory/cpu|cpu/memory|memory and cpu|cpu and memory|memory|cpu)"
        if re.fullmatch(
            rf"(?:(?:show|check|read|report)(?: me)? (?:my |the )?|"
            rf"what(?:'s|s| is| are) (?:my |the )?|)"
            rf"(?:current )?{performance_metric} (?:usage|utilization)"
            rf"(?: right now| currently)?",
            routed,
        ) or re.fullmatch(
            rf"how much (?:of (?:my |the )?)?{performance_metric} "
            rf"(?:am i|is (?:my |the )?(?:computer|device|system)) using",
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
        if re.fullmatch(
            r"(?:show|list|manage) (?:my |the )?"
            r"(?:(?:connected|paired) devices|devices)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "connected_devices"},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(enable|disable) (?:connected )?device (.+)",
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
        if re.fullmatch(
            r"(?:play|pause|resume|play pause)(?: the)? (?:music|playback)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.media",
                    arguments={"action": "play_pause"},
                    confidence=0.95,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:next|skip)(?: the| this)? (?:song|track)|"
            r"(?:skip|go) to (?:the )?next (?:song|track)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.media",
                    arguments={"action": "next"},
                    confidence=0.97,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:previous|last|back)(?: the| this)? (?:song|track)|"
            r"(?:go|skip) (?:back )?to (?:the )?(?:previous|last) (?:song|track)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.media",
                    arguments={"action": "previous"},
                    confidence=0.97,
                    source="rule",
                )
            )
        if re.fullmatch(r"stop(?: the)? (?:music|playback)", routed):
            return RouteResult(
                Intent(
                    name="windows.media",
                    arguments={"action": "stop"},
                    confidence=0.97,
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
        if routed in {
            "volume up",
            "turn volume up",
            "turn up the volume",
            "increase volume",
            "increase the volume",
            "raise the volume",
            "louder",
        }:
            return RouteResult(
                Intent(
                    name="windows.media",
                    arguments={"action": "volume_up"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if routed in {
            "volume down",
            "turn volume down",
            "turn down the volume",
            "decrease volume",
            "decrease the volume",
            "lower the volume",
            "quieter",
        }:
            return RouteResult(
                Intent(
                    name="windows.media",
                    arguments={"action": "volume_down"},
                    confidence=0.99,
                    source="rule",
                )
            )
        return None
