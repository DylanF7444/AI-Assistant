from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from assistant.contracts import Intent
from assistant.language.planner import LocalPlanner, PlannerDecision


@dataclass(frozen=True)
class PlannerResolution:
    decision: PlannerDecision | None = None
    intent: Intent | None = None
    error: Exception | None = None
    unavailable: bool = False


class PlannerCoordinator:
    async def resolve(
        self,
        planner: LocalPlanner | None,
        text: str,
        routed_intent: Intent | None,
        session_context: tuple[dict[str, str], ...],
        *,
        interpret_goal: Callable[[], Awaitable[PlannerDecision | None]],
    ) -> PlannerResolution:
        if planner is None:
            return PlannerResolution(unavailable=True)
        try:
            if (
                routed_intent is not None
                and routed_intent.name == "planner.goal"
                and hasattr(planner, "interpret")
            ):
                decision = await interpret_goal()
                if decision is not None:
                    return PlannerResolution(decision=decision)
            if hasattr(planner, "plan_decision"):
                return PlannerResolution(
                    decision=await planner.plan_decision(text, session_context)
                )
            return PlannerResolution(intent=await planner.plan(text, session_context))
        except Exception as exc:
            return PlannerResolution(error=exc)
