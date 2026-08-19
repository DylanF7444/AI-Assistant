from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class WorkBriefHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        if re.fullmatch(
            r"(?:(?:brief me|(?:build|create)(?: me)? (?:my )?work brief) "
            r"(?:and )?save(?: it)?)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.work_brief",
                    arguments={"delivery": "assistant", "save": True},
                    confidence=1,
                    source="deterministic",
                )
            )
        if re.fullmatch(
            r"(?:brief me|(?:build|create|give|show)(?: me)? (?:my )?work brief)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.work_brief",
                    arguments={"delivery": "assistant"},
                    confidence=1,
                    source="deterministic",
                )
            )
        if re.fullmatch(
            r"(?:(?:brief me|(?:build|create|give|show)(?: me)? (?:my )?work brief) "
            r"(?:and )?(?:save|send|put)(?: it)? (?:to|in) (?:my )?inbox)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.work_brief",
                    arguments={"delivery": "inbox"},
                    confidence=1,
                    source="deterministic",
                )
            )
        return None
