from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class FileOperationHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        if match := re.fullmatch(
            r"(?P<operation>copy(?: and paste)?|move|rename) "
            r"(?P<source>.+?) "
            r"(?P<preposition>to|onto|into|in|as) "
            r"(?P<destination>.+)",
            routed,
        ):
            operation = match.group("operation")
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={
                        "operation": "copy" if operation.startswith("copy") else operation,
                        "source": match.group("source"),
                        "destination": match.group("destination"),
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
        if match := re.fullmatch(
            r"open (?P<source>(?:the |this |that )?"
            r"(?:(?:\d+)(?:st|nd|rd|th)?|first|second|third|fourth|fifth|last)"
            r"(?: file| item| result| match| one)?)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={
                        "operation": "open",
                        "source": match.group("source").strip(),
                    },
                    confidence=0.94,
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
        return None
