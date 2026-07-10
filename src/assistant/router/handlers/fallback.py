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
        normalized = context.normalized
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
