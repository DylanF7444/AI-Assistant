from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.handlers.common import has_explicit_file_extension
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


def _looks_like_file_request(text: str) -> bool:
    folded = text.strip().casefold()
    return bool(
        re.search(r"\b(?:file|document|folder)\b", folded)
        or re.search(
            r"\b(?:in|inside|on|from) (?:my |the )?"
            r"(?:desktop|documents?|downloads?|pictures?|music|videos?)\b",
            folded,
        )
        or re.match(
            r"^(?:[a-z]:[\\/]|\\\\|/|~[\\/])",
            folded,
            flags=re.IGNORECASE,
        )
        or has_explicit_file_extension(folded)
    )


class NetworkHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        text = context.text
        routed = context.routed
        if match := re.fullmatch(
            r"(?:(?:search|look on) (?:the )?(?:web|internet|online) for|"
            r"web search for|look up|google|bing|duckduckgo) (.+)",
            routed,
        ):
            if _looks_like_file_request(match.group(1)):
                return None
            return RouteResult(
                Intent(
                    name="web.search",
                    arguments={"query": match.group(1), "count": 5},
                    confidence=0.99,
                    source="rule",
                )
            )
        if headless_request := self._route_result_oriented_browse(routed, router):
            return RouteResult(headless_request)
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
        if match := re.fullmatch(
            r"(?:open|visit|go to|navigate to|browse to) (https://\S+)",
            text.strip(),
            re.I,
        ):
            return RouteResult(
                Intent(
                    name="windows.browser_navigate",
                    arguments={"url": match.group(1)},
                    confidence=0.99,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:open|visit|go to|navigate to|browse to) (.+)",
            routed,
        ):
            target = match.group(1).strip()
            if not _looks_like_file_request(target) and (url := router._website_url(target)):
                return RouteResult(
                    Intent(
                        name="windows.browser_navigate",
                        arguments={"url": url},
                        confidence=0.97,
                        source="rule",
                    )
                )
        return None

    @staticmethod
    def _route_result_oriented_browse(
        routed: str,
        router: DeterministicRouter,
    ) -> Intent | None:
        patterns = (
            r"(?:go to|visit|browse|open) (?P<website>.+?) "
            r"(?:and(?: then)?|then) (?:(?:give|show|tell) me|find|get|read) "
            r"(?P<extract>.+)",
            r"(?:(?:give|show|tell) me|find|get|read) (?P<extract>.+?) "
            r"(?:from|on|at) (?P<website>.+)",
        )
        for pattern in patterns:
            match = re.fullmatch(pattern, routed, flags=re.I)
            if match is None:
                continue
            url = router._website_url(match.group("website"))
            if url is None:
                continue
            raw_extract = match.group("extract").strip()
            singular = bool(
                re.search(r"\b(?:first|latest|newest|top|one|1st)\b", raw_extract, re.I)
            )
            extract = re.sub(
                r"^(?:(?:the|a|an)\s+)?(?:first|latest|newest|top|one|1st)\s+",
                "",
                raw_extract,
                flags=re.I,
            ).strip()
            if not extract:
                continue
            return Intent(
                name="web.headless_read",
                arguments={
                    "url": url,
                    "max_characters": 12000,
                    "max_links": 30,
                    "timeout_seconds": 30,
                    "wait_after_load_ms": 750,
                    "extract": extract,
                    "max_items": 1 if singular else 5,
                },
                confidence=0.99,
                source="rule",
            )
        return None
