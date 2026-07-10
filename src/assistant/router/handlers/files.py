from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class FileMutationHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        if match := re.fullmatch(
            r"copy(?: and paste)? "
            r"(?:it|that|the selected file|this file)(?: to| onto| into| in) (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={
                        "operation": "copy",
                        "source": "${selected_file}",
                        "destination": f"{match.group(1).strip()}/${{selected_file_name}}",
                    },
                    confidence=0.95,
                    source="rule",
                )
            )
        if re.fullmatch(r"delete (?:it|that|the selected file|this file)", routed):
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={"operation": "delete", "source": "${selected_file}"},
                    confidence=0.95,
                    source="rule",
                )
            )
        if re.fullmatch(r"open (?:it|that|the selected file|this file)", routed):
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={"operation": "open", "source": "${selected_file}"},
                    confidence=0.95,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:add|create|make) (?:a |an )?(?:new |blank )?(?:text|txt) file"
            r"(?: (?:named|called) ([a-z0-9][a-z0-9_. -]*))?"
            r" (?:to|in|inside|into) (.+)",
            routed,
        ):
            target = router._file_destination_folder(match.group(2))
            if target:
                filename = router._text_filename(match.group(1))
                return RouteResult(
                    Intent(
                        name="windows.file_operation",
                        arguments={
                            "operation": "create_text",
                            "source": f"{target}/{filename}",
                        },
                        confidence=0.94,
                        source="rule",
                    )
                )
        return None
