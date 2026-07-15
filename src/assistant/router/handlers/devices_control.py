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
        if routed in {
            "lock device",
            "lock the device",
            "lock my device",
            "lock computer",
            "lock the computer",
        }:
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "lock_device"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if routed in {
            "restart device",
            "restart the device",
            "restart computer",
            "restart the computer",
            "reboot device",
            "reboot computer",
        }:
            return RouteResult(
                Intent(
                    name="windows.device_control",
                    arguments={"operation": "restart_device"},
                    confidence=0.99,
                    source="rule",
                )
            )
        if routed in {
            "shut down device",
            "shutdown device",
            "shut down the device",
            "shutdown the device",
            "shut down computer",
            "shutdown computer",
        }:
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
        return None
