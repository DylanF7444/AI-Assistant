from __future__ import annotations

from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class FallbackHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        text = context.text
        return RouteResult(
            Intent(
                name="planner.goal",
                arguments={"text": text},
                confidence=0.7,
                source="classifier",
                ambiguous=True,
                ambiguity_reasons=("Open-ended input requires goal interpretation",),
            ),
            needs_llm=True,
        )
