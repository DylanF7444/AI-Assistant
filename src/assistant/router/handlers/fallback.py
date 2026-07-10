from __future__ import annotations

from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.handlers.common import looks_like_natural_request
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
        natural_request = looks_like_natural_request(normalized)
        return RouteResult(
            Intent(
                name=(
                    "planner.goal"
                    if natural_request
                    else ("conversation.question" if question else "planner.request")
                ),
                arguments={"text": text},
                confidence=0.7 if natural_request else 0.5,
                source="classifier",
                ambiguous=True,
                ambiguity_reasons=(
                    "Natural-language request requires goal interpretation"
                    if natural_request
                    else "No deterministic rule matched",
                ),
            ),
            needs_llm=True,
        )
