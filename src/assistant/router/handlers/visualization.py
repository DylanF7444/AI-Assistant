from __future__ import annotations

import re

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult
from assistant.visualization import parse_visual_request, visual_request_kind


_DATA_ACQUISITION_CUES = re.compile(
    r"\b(?:current|latest|newest|recent|today|this\s+(?:week|month|quarter|year)|"
    r"top|bottom|highest|lowest|largest|smallest|most|least|best|worst|"
    r"earnings?|gross(?:ing)?|box\s+office|revenue|statistics?|stats?|"
    r"rank(?:ed|ing|s)?|prices?|scores?|standings?|results?)\b",
    re.IGNORECASE,
)


class VisualizationHandler:
    """Route explicit visual intent through the shared natural-language parser."""

    def route(self, context: RouteContext, router) -> RouteResult | None:
        kind = visual_request_kind(context.text)
        if kind is None:
            return None
        parsed = parse_visual_request(context.text, kind=kind)
        if parsed is None:
            return None
        if parsed.clarification:
            if _DATA_ACQUISITION_CUES.search(context.text):
                return RouteResult(
                    Intent(
                        name="planner.goal",
                        arguments={"text": context.text},
                        confidence=0.85,
                        source="classifier",
                        ambiguous=True,
                        ambiguity_reasons=(
                            "Visualization requires data acquisition before rendering",
                        ),
                    ),
                    needs_llm=True,
                )
            return RouteResult(None, clarification=parsed.clarification)
        return RouteResult(
            Intent(
                name="core.visualize",
                arguments={"title": parsed.title, "spec": parsed.spec},
                confidence=0.98,
                source="rule",
            )
        )
