from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from assistant.contracts import Intent

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


@dataclass(frozen=True)
class RouteResult:
    intent: Intent | None
    intents: tuple[Intent, ...] = ()
    needs_llm: bool = False
    clarification: str | None = None


@dataclass(frozen=True)
class RouteContext:
    text: str
    normalized: str
    routed: str


class RouteHandler(Protocol):
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None: ...
