from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

VoicePromptKind = Literal[
    "approval",
    "permission_duration",
    "permission_targets",
    "critical_token",
]


@dataclass(frozen=True)
class VoicePromptResponse:
    action: Literal[
        "approve",
        "deny",
        "once",
        "session",
        "always",
        "requested_targets",
        "any_target",
        "token",
    ]
    value: str | None = None


def keyword_request(text: str, keyword: str) -> str | None:
    """Return request text after a keyword, empty string for keyword-only activation."""

    normalized_keyword = " ".join(keyword.split())
    if not normalized_keyword:
        return None
    request_text = text.strip()
    if not request_text:
        return None
    keyword_pattern = r"\s+".join(re.escape(part) for part in normalized_keyword.split())
    pattern = re.compile(rf"\b{keyword_pattern}\b[,:-]?\s*", re.IGNORECASE)
    match = pattern.search(request_text)
    if not match:
        return None
    return request_text[match.end() :].strip()


def is_explicit_cancel_command(text: str) -> bool:
    """Return whether speech is an unambiguous request to cancel current work."""

    spoken = " ".join(re.findall(r"[a-z0-9']+", text.casefold()))
    return bool(
        re.fullmatch(
            r"(?:hey\s+)?(?:assistant\s+)?(?:please\s+)?"
            r"(?:cancel|stop)"
            r"(?:\s+(?:that|this|it|now|processing|the\s+current\s+request|"
            r"current\s+request|the\s+current\s+command|current\s+command))?"
            r"(?:\s+please)?",
            spoken,
        )
    )


class ClapDetector:
    def __init__(self, *, threshold: float = 0.35, cooldown_blocks: int = 20) -> None:
        self.threshold = threshold
        self.cooldown_blocks = cooldown_blocks
        self._cooldown = 0
        self._previous_level = 0.0

    def observe(self, level: float) -> bool:
        if self._cooldown > 0:
            self._cooldown -= 1
            self._previous_level = level
            return False
        quiet_before = self._previous_level < self.threshold * 0.35
        detected = level >= self.threshold and quiet_before
        self._previous_level = level
        if detected:
            self._cooldown = self.cooldown_blocks
        return detected


def parse_voice_prompt_response(
    text: str,
    kind: VoicePromptKind,
    *,
    expected_token: str | None = None,
) -> VoicePromptResponse | None:
    spoken = _normalize_spoken_choice(text)
    if not spoken:
        return None
    if _contains_any(spoken, ("cancel", "stop", "no", "deny", "decline", "not now")):
        return VoicePromptResponse("deny")
    if kind == "approval":
        if _contains_any(spoken, ("yes", "approve", "confirm", "grant", "continue", "run it")):
            return VoicePromptResponse("approve")
        return None
    if kind == "permission_duration":
        if _contains_any(spoken, ("just once", "one time", "only once", "once")):
            return VoicePromptResponse("once")
        if _contains_any(spoken, ("this session", "session", "until i quit")):
            return VoicePromptResponse("session")
        if _contains_any(spoken, ("always", "permanent", "remember", "from now on")):
            return VoicePromptResponse("always")
        return None
    if kind == "permission_targets":
        if _contains_any(spoken, ("any target", "all targets", "everything", "anywhere", "all")):
            return VoicePromptResponse("any_target")
        if _contains_any(
            spoken,
            (
                "requested target",
                "requested targets",
                "only this",
                "this target",
                "these targets",
                "exact target",
                "exact targets",
            ),
        ):
            return VoicePromptResponse("requested_targets")
        return None
    if kind == "critical_token":
        if not expected_token:
            return None
        token = _spoken_token(spoken)
        if token == expected_token.casefold():
            return VoicePromptResponse("token", expected_token)
    return None


def _normalize_spoken_choice(text: str) -> str:
    return " ".join(text.casefold().replace("-", " ").split())


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(phrase)}\b", text) for phrase in phrases)


def _spoken_token(text: str) -> str:
    words = {
        "zero": "0",
        "oh": "0",
        "o": "0",
        "one": "1",
        "two": "2",
        "to": "2",
        "too": "2",
        "three": "3",
        "four": "4",
        "for": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "ate": "8",
        "nine": "9",
    }
    pieces = []
    for part in re.findall(r"[a-z0-9]+", text):
        pieces.append(words.get(part, part))
    return "".join(pieces)
