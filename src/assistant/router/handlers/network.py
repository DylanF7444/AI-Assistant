from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class NetworkHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        text = context.text
        routed = context.routed
        if match := re.fullmatch(r"(?:search the web|web search) for (.+)", routed):
            return RouteResult(
                Intent(
                    name="web.search",
                    arguments={"query": match.group(1), "count": 5},
                    confidence=0.99,
                    source="rule",
                )
            )
        headless_patterns = (
            r"(?:read|render|fetch) (https://\S+) "
            r"(?:in the background|headlessly|with (?:a )?headless browser)",
            r"(?:headlessly|in the background) (?:read|render|fetch) (https://\S+)",
            r"(?:read|render|fetch) (?:the )?rendered page (https://\S+)",
        )
        for pattern in headless_patterns:
            if match := re.fullmatch(pattern, text.strip(), re.I):
                return RouteResult(
                    Intent(
                        name="web.headless_read",
                        arguments={
                            "url": match.group(1),
                            "max_characters": 12000,
                            "max_links": 30,
                            "timeout_seconds": 30,
                            "wait_after_load_ms": 250,
                        },
                        confidence=0.99,
                        source="rule",
                    )
                )
        if match := re.fullmatch(r"(?:read|fetch) (https://\S+)", text.strip(), re.I):
            return RouteResult(
                Intent(
                    name="web.fetch",
                    arguments={"url": match.group(1), "max_characters": 12000},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"open (https://\S+)", text.strip(), re.I):
            return RouteResult(
                Intent(
                    name="windows.browser_navigate",
                    arguments={"url": match.group(1)},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"ask (chatgpt|codex|openai|claude) (?:for )?(.+)", routed):
            provider = {
                "claude": "anthropic",
                "codex": "codex",
            }.get(match.group(1), "openai")
            arguments = {
                "provider": provider,
                "question": match.group(2),
                "max_tokens": 1500,
            }
            return RouteResult(
                Intent(
                    name="cloud.ask",
                    arguments=arguments,
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:make|write|generate|create) (?:some )?(?:code|a script|an app|a program)(?: (?:to|for|that|which))? (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="cloud.ask",
                    arguments={
                        "provider": "codex",
                        "question": f"Write code {match.group(1)}",
                        "max_tokens": 4000,
                    },
                    confidence=0.96,
                    source="rule",
                )
            )
        if match := re.fullmatch(r"code (?:up|me|for me) (.+)", routed):
            return RouteResult(
                Intent(
                    name="cloud.ask",
                    arguments={
                        "provider": "codex",
                        "question": f"Write code for {match.group(1)}",
                        "max_tokens": 4000,
                    },
                    confidence=0.94,
                    source="rule",
                )
            )
        return None
