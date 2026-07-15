from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class FileSearchHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        patterns = (
            # Location first: "look in Desktop for a file named resume".
            r"(?:look|search|check) (?:in|on|through) (?:my |the )?"
            r"(?P<root>.+?) for (?:a |the |my )?"
            r"(?:(?:file|document)(?: named| called)? )?"
            r"(?P<query>.+)",
            # Location first without a preposition requires an explicit file noun so
            # ordinary web/application searches do not become filesystem searches.
            r"(?:look|search|check) (?:my |the )?(?P<root>.+?) for "
            r"(?:a |the |my )?(?:file|document)(?: named| called)? (?P<query>.+)",
            # Target first with an explicit file noun.
            r"(?:find|locate|search for|look for) (?:a |the |my )?"
            r"(?:file|document)(?: named| called)? (?P<query>.+?) "
            r"(?:in|on) (?:my |the )?(?P<root>.+)",
            # Target first, as in "find my resume on my desktop".
            r"(?:find|locate|search for|look for) (?:my |the )?(?P<query>.+?) "
            r"(?:in|on) (?:my |the )?(?P<root>.+)",
        )
        for pattern in patterns:
            if match := re.fullmatch(pattern, routed):
                root = _normalized_file_search_root(match.group("root"))
                if root is None:
                    continue
                return RouteResult(
                    Intent(
                        name="windows.file_search",
                        arguments={
                            "query": match.group("query").strip().strip("\"'"),
                            "root": root,
                            "limit": 20,
                        },
                        confidence=0.9,
                        source="rule",
                        ambiguous=True,
                        ambiguity_reasons=("filesystem path must be resolved and approved",),
                    )
                )
        return None


def _is_explicit_file_search_root(root: str) -> bool:
    normalized = re.sub(r"^(?:my|the)\s+", "", root.strip(), flags=re.IGNORECASE)
    normalized = re.sub(r"\s+folder$", "", normalized, flags=re.IGNORECASE)
    first = normalized.replace("\\", "/").partition("/")[0].casefold()
    if first in {
        "desktop",
        "documents",
        "document",
        "downloads",
        "download",
        "pictures",
        "picture",
        "music",
        "videos",
        "video",
    }:
        return True
    return bool(re.match(r"^(?:[a-z]:[\\/]|\\\\|/|~[\\/]|\$\{user_[a-z]+\})", normalized, re.I))


def _normalized_file_search_root(root: str) -> str | None:
    """Return a safe, resolvable root for an explicit user folder reference.

    This understands named folders relative to a known user folder, rather than
    maintaining phrases for individual folder names such as "Assistant".
    """
    value = root.strip().strip("\"'").rstrip(".").strip()
    if _is_explicit_file_search_root(value):
        return re.sub(r"^(?:my|the)\s+", "", value, flags=re.IGNORECASE)

    known_folder = r"desktop|documents?|downloads?|pictures?|music|videos?"
    relative_patterns = (
        rf"(?:my |the )?(?P<child>.+?)\s+folder\s+"
        rf"(?:in|on|inside|under)\s+(?:my |the )?(?P<parent>{known_folder})",
        rf"(?:my |the )?(?P<parent>{known_folder})\s*[/\\]\s*"
        rf"(?P<child>.+?)(?:\s+folder)?",
        rf"(?:the )?(?P<child>.+?)\s+folder\s+inside\s+"
        rf"(?:my |the )?(?P<parent>{known_folder})\s+folder",
    )
    for pattern in relative_patterns:
        match = re.fullmatch(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        child = match.group("child").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9 _.-]{0,99}", child, flags=re.IGNORECASE):
            return None
        parent = match.group("parent").casefold()
        parent = {
            "document": "documents",
            "download": "downloads",
            "picture": "pictures",
            "video": "videos",
        }.get(parent, parent)
        return f"{parent}/{child}"
    return None


class FileMutationHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        if match := re.fullmatch(
            r"copy(?: and paste)? "
            r"(?:it|that|the selected file|this file)(?: to| onto| into| in) (.+)",
            routed,
        ):
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={
                        "operation": "copy",
                        "source": "${selected_file}",
                        "destination": f"{match.group(1).strip()}/${{selected_file_name}}",
                    },
                    confidence=0.95,
                    source="rule",
                )
            )
        if re.fullmatch(r"delete (?:it|that|the selected file|this file)", routed):
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={"operation": "delete", "source": "${selected_file}"},
                    confidence=0.95,
                    source="rule",
                )
            )
        if re.fullmatch(r"open (?:it|that|the selected file|this file)", routed):
            return RouteResult(
                Intent(
                    name="windows.file_operation",
                    arguments={"operation": "open", "source": "${selected_file}"},
                    confidence=0.95,
                    source="rule",
                )
            )
        if match := re.fullmatch(
            r"(?:add|create|make) (?:a |an )?(?:new |blank )?(?:text|txt) file"
            r"(?: (?:named|called) ([a-z0-9][a-z0-9_. -]*))?"
            r" (?:to|in|inside|into) (.+)",
            routed,
        ):
            target = router._file_destination_folder(match.group(2))
            if target:
                filename = router._text_filename(match.group(1))
                return RouteResult(
                    Intent(
                        name="windows.file_operation",
                        arguments={
                            "operation": "create_text",
                            "source": f"{target}/{filename}",
                        },
                        confidence=0.94,
                        source="rule",
                    )
                )
        return None
