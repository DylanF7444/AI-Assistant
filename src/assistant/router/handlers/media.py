from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from assistant.contracts import Intent, MediaKind
from assistant.presentation.media_probe import SUPPORTED_MEDIA_EXTENSIONS
from assistant.presentation.media_resolver import is_explicit_media_path
from assistant.presentation.remote_media import is_explicit_remote_media_url
from assistant.router.handlers.files import _normalized_file_search_root
from assistant.router.normalization import spoken_number
from assistant.router.types import RouteContext, RouteResult

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter

_MEDIA_NOUN = (
    r"photo|photograph|picture|image|audio|audio file|recording|song|track|"
    r"video|video file|movie|clip"
)
_SELECTED_REFERENCE = re.compile(
    r"^(?:it|that|this|the selected (?:file|item)|"
    r"(?:the )?(?:(?:\d+)(?:st|nd|rd|th)?|first|second|third|fourth|fifth|last)"
    r"(?: file| item| result| match| one)?)$",
    flags=re.IGNORECASE,
)


class MediaHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        if result := _viewer_control_route(routed, router):
            return result
        match = re.fullmatch(
            r"(?P<verb>show|display|view|pull up|play)"
            r"(?: me)?(?: the| my)? (?P<source>.+)",
            routed,
        )
        if not match:
            return None
        verb = match.group("verb")
        source = _original_media_source(context.text, verb)
        if verb == "play" and source is not None and source.casefold() == "music":
            return None
        if source is None or not _looks_like_media_reference(source):
            return None
        arguments = _presentation_arguments(source)
        arguments["autoplay"] = verb == "play"
        exact_source = is_explicit_media_path(source) or is_explicit_remote_media_url(source)
        return RouteResult(
            Intent(
                name="media.present",
                arguments=arguments,
                confidence=0.96,
                source="rule",
                ambiguous=not exact_source,
                ambiguity_reasons=(
                    ("Media reference must resolve to one approved file.",)
                    if not exact_source
                    else ()
                ),
            )
        )


def _original_media_source(text: str, verb: str) -> str | None:
    match = re.match(
        rf"^\s*{re.escape(verb)}(?:\s+me)?(?:\s+(?:the|my))?\s+(?P<source>.+?)\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group("source").strip().strip("\"'")


def _looks_like_media_reference(source: str) -> bool:
    folded = source.strip().casefold()
    if folded.startswith(("how ", "why ", "what ", "information ", "details ")):
        return False
    if re.search(r"\b(?:photo|picture|image|video|movie|clip)\s+of\b", folded):
        return False
    if _SELECTED_REFERENCE.fullmatch(folded):
        return True
    if is_explicit_media_path(source):
        return True
    if is_explicit_remote_media_url(source):
        return True
    if any(folded.endswith(extension) for extension in SUPPORTED_MEDIA_EXTENSIONS):
        return True
    if re.search(rf"\b(?:{_MEDIA_NOUN})\b", folded):
        location = re.search(
            r"\b(?:in|inside|from|on)\s+(?:my\s+|the\s+)?"
            r"(?:camera roll|desktop|documents?|downloads?|pictures?|music|videos?)\b",
            folded,
        )
        if location is not None:
            return True
        explicit_location = re.search(
            r"\b(?:in|inside|from|on)\s+(?P<root>.+)$",
            source,
            flags=re.IGNORECASE,
        )
        return bool(explicit_location and is_explicit_media_path(explicit_location.group("root")))
    return False


def _presentation_arguments(source: str) -> dict[str, Any]:
    if _SELECTED_REFERENCE.fullmatch(source.strip()):
        return {"source": source}
    match = re.fullmatch(
        r"(?P<query>.+?)\s+(?:in|inside|from|on)\s+(?P<root>.+)",
        source,
        flags=re.IGNORECASE,
    )
    if match:
        root_text = re.sub(
            r"\s+folder$",
            "",
            match.group("root").strip(),
            flags=re.IGNORECASE,
        )
        root = _normalized_media_root(root_text)
        if root is not None:
            query = _normalized_media_query(match.group("query"))
            kind = _kind_hint(source)
            if query or kind is not None:
                arguments: dict[str, Any] = {"root": root, "limit": 20}
                if query:
                    arguments["query"] = query
                if kind is not None:
                    arguments["kind_hint"] = kind.value
                return arguments
    arguments = {"source": source}
    if kind := _kind_hint(source):
        arguments["kind_hint"] = kind.value
    return arguments


def _normalized_media_query(value: str) -> str:
    query = re.sub(
        r"^(?:the|my|a|an)\s+",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        rf"\s+(?:{_MEDIA_NOUN})$",
        "",
        query,
        flags=re.IGNORECASE,
    ).strip()
    if re.fullmatch(rf"(?:{_MEDIA_NOUN})", cleaned, flags=re.IGNORECASE):
        return ""
    return cleaned or query


def _normalized_media_root(value: str) -> str | None:
    folded = re.sub(
        r"^(?:my|the)\s+",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    ).casefold()
    if folded == "camera roll":
        return "Pictures/Camera Roll"
    return _normalized_file_search_root(value)


def _kind_hint(value: str) -> MediaKind | None:
    folded = value.casefold()
    if (
        is_explicit_media_path(value)
        or is_explicit_remote_media_url(value)
        or any(folded.endswith(extension) for extension in SUPPORTED_MEDIA_EXTENSIONS)
    ):
        return None
    if re.search(r"\b(?:photo|photograph|picture|image)\b", folded):
        return MediaKind.IMAGE
    if re.search(r"\b(?:audio|audio file|recording|song|track)\b", folded):
        return MediaKind.AUDIO
    if re.search(r"\b(?:video|video file|movie|clip)\b", folded):
        return MediaKind.VIDEO
    return None


def _viewer_control_route(
    routed: str,
    router: DeterministicRouter,
) -> RouteResult | None:
    simple_actions = (
        (
            "play",
            r"(?:play|resume) (?:it|"
            r"(?:(?:this|the|current) )?(?:video|audio|media|movie|clip|viewer))",
        ),
        (
            "pause",
            r"pause (?:it|"
            r"(?:(?:this|the|current) )?(?:video|audio|media|movie|clip|viewer))",
        ),
        (
            "stop",
            r"stop(?: (?:this|the|current))?"
            r"(?: (?:video|audio|media|movie|clip|viewer))|stop it",
        ),
        (
            "toggle_mute",
            r"(?:mute|unmute)(?: (?:this|the|current))?"
            r" (?:video|audio|media|movie|clip|viewer)",
        ),
        (
            "fullscreen",
            r"(?:make|put) (?:it|this|the (?:video|viewer)) (?:go )?full ?screen|"
            r"(?:go |enter )?full ?screen",
        ),
        (
            "exit_fullscreen",
            r"(?:exit|leave) full ?screen",
        ),
        (
            "close",
            r"(?:close|hide|dismiss) (?:the |this )?(?:media )?viewer",
        ),
    )
    for action, pattern in simple_actions:
        if re.fullmatch(pattern, routed):
            return _control_result(action)

    if match := re.fullmatch(
        r"(?:skip|seek|go) (?:ahead|forward|forwards|back|backward|backwards)"
        r"(?: by)? (?P<duration>.+)",
        routed,
    ):
        direction = -1 if re.search(r"\bback", routed) else 1
        seconds = _duration_seconds(match.group("duration"))
        if seconds is not None and seconds > 0:
            return _control_result(
                "seek_relative",
                delta_seconds=direction * seconds,
            )
    if match := re.fullmatch(
        r"(?:seek|go|jump) to (?P<position>.+)",
        routed,
    ):
        seconds = _duration_seconds(match.group("position"))
        if seconds is not None:
            return _control_result("seek_absolute", position_seconds=seconds)
    if match := re.fullmatch(
        r"(?:set|change) (?:the )?(?:viewer |media |playback )?volume"
        r"(?: to)? (?P<volume>.+)",
        routed,
    ):
        percent = router._percent(match.group("volume"))
        if percent is not None:
            return _control_result("set_volume", volume=percent / 100)
    if match := re.fullmatch(
        r"(?:set|change) (?:the )?(?:playback )?speed(?: to)? (?P<rate>\d+(?:\.\d+)?)x?",
        routed,
    ):
        rate = float(match.group("rate"))
        if 0.25 <= rate <= 4:
            return _control_result("set_rate", playback_rate=rate)
    return None


def _control_result(action: str, **arguments: Any) -> RouteResult:
    return RouteResult(
        Intent(
            name="media.viewer_control",
            arguments={"action": action, **arguments},
            confidence=0.97,
            source="rule",
        )
    )


def _duration_seconds(value: str) -> float | None:
    normalized = value.strip().casefold()
    if match := re.fullmatch(r"(?P<minutes>\d+):(?P<seconds>\d{1,2})", normalized):
        seconds = int(match.group("seconds"))
        if seconds >= 60:
            return None
        return int(match.group("minutes")) * 60 + seconds
    total = 0.0
    matched_unit = False
    for amount, unit in re.findall(
        r"(\d+(?:\.\d+)?|[a-z-]+(?:\s+[a-z-]+)*?)\s+"
        r"(hours?|minutes?|seconds?)\b",
        normalized,
    ):
        try:
            number = spoken_number(amount)
        except ValueError:
            continue
        matched_unit = True
        total += (
            number
            * {
                "hour": 3600,
                "hours": 3600,
                "minute": 60,
                "minutes": 60,
                "second": 1,
                "seconds": 1,
            }[unit]
        )
    if matched_unit:
        return total
    try:
        return spoken_number(re.sub(r"\s*(?:seconds?|secs?)$", "", normalized).strip())
    except ValueError:
        return None
