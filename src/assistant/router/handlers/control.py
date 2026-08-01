from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter

from assistant.router.handlers.common import (
    memory_key,
    memory_type,
    memory_value_from_original,
)


class ControlHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        if routed in router.CONTROL:
            return RouteResult(
                Intent(
                    name=router.CONTROL[routed],
                    confidence=1,
                    source="deterministic",
                )
            )
        if match := re.fullmatch(r"correct that to (.+)", routed):
            return RouteResult(
                Intent(
                    name="control.correct",
                    arguments={"text": match.group(1)},
                    confidence=1,
                    source="deterministic",
                )
            )
        if match := re.fullmatch(
            r"(?:select|choose|pick) (?:file |result |item |number )?(.+)", routed
        ):
            return RouteResult(
                Intent(
                    name="control.result_select",
                    arguments={"selector": match.group(1).strip()},
                    confidence=0.95,
                    source="rule",
                )
            )
        return None


class MemoryConversationHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        text = context.text
        routed = context.routed
        if match := re.fullmatch(r"save that (.+)", routed):
            return RouteResult(
                Intent(
                    name="core.notes",
                    arguments={
                        "operation": "add",
                        "title": "Saved information",
                        "body": match.group(1),
                    },
                    confidence=0.98,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:please )?remember (?:that .+|"
            r"(?:my|i(?:'m| am| have| like| love| prefer| use)|"
            r"call me|refer to me as) .+)",
            routed,
        ):
            value = memory_value_from_original(text)
            return RouteResult(
                Intent(
                    name="core.memory",
                    arguments={
                        "operation": "remember",
                        "type": memory_type(value),
                        "key": memory_key(value),
                        "value": value,
                    },
                    confidence=0.98,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:what do you remember(?: about me)?|"
            r"(?:show|list) (?:what you remember|(?:my )?memories))",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="core.memory",
                    arguments={"operation": "list"},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:forget|delete) (?:memory|memory number) (\d+)",
            routed,
        ):
            operation = "delete" if routed.startswith("delete ") else "forget"
            return RouteResult(
                Intent(
                    name="core.memory",
                    arguments={"operation": operation, "memory_id": int(match.group(1))},
                    confidence=0.98,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"forget that (.+)", routed):
            return RouteResult(
                Intent(
                    name="core.memory",
                    arguments={"operation": "forget", "query": match.group(1)},
                    confidence=0.85,
                    source="rule",
                    ambiguous=True,
                    ambiguity_reasons=("Memory lookup must resolve this description",),
                )
            )
        if routed in {
            "run diagnostics",
            "diagnostics",
            "check assistant health",
            "system status",
        }:
            return RouteResult(
                Intent(
                    name="core.diagnostic",
                    arguments={"include_skills": True},
                    confidence=1,
                    source="deterministic",
                )
            )
        if re.fullmatch(
            r"(?:how do i |how can i |where do i |where can i )?"
            r"(?:register|add) (?:an? |the )?(?:application|app)"
            r"(?: so you can open it(?: for me)?| for launching| to open)?",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="conversation.answer",
                    arguments={"text": router.APP_REGISTRATION_HELP},
                    confidence=0.99,
                    source="rule",
                )
            )
        if re.fullmatch(
            r"(?:list|show|what are|what're) (?:my |the )?"
            r"registered (?:apps|applications|programs)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.registered_applications",
                    arguments={},
                    confidence=0.99,
                    source="rule",
                )
            )
        return None
