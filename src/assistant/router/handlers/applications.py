from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult
from assistant.windows.process_names import executable_from_command_name

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class ApplicationWindowHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        if (
            match := re.fullmatch(
                r"(?:move|place|put|switch) (.+?) (?:window )?(?:to|on) (?:my |the )?(?:(?:monitor|screen|display) )?(.+?)(?: (?:monitor|screen|display))?",
                routed,
            )
        ) and (monitor := router._monitor_index(match.group(2))):
            return RouteResult(
                Intent(
                    name="windows.application_window",
                    arguments={
                        "operation": "move_to_monitor",
                        "process_name": executable_from_command_name(match.group(1)),
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
        return None
