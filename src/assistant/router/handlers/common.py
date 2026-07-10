from __future__ import annotations

import re


def looks_like_goal_request(text: str) -> bool:
    return bool(
        re.search(
            r"\b("
            r"help me|clean up|organize|prepare|figure out|make .* easier|"
            r"what should i|working on yesterday|compare|summarize|rewrite|"
            r"make this note|note into tasks|turn that down|plan|fix this|"
            r"remember that|use my usual"
            r")\b",
            text,
            re.I,
        )
    )


def looks_like_natural_request(text: str) -> bool:
    """Recognize request framing without guessing the requested capability."""
    normalized = " ".join(text.casefold().replace("’", "'").split())
    return bool(
        re.match(
            r"^(?:please\b|"
            r"i\s+(?:need|want|would like|wish)\b|"
            r"i[' ]d\s+like\b|"
            r"(?:can|could|would|will)\s+you\b)",
            normalized,
        )
    )


def memory_key(value: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", value.casefold()))[:200]


def memory_type(value: str) -> str:
    folded = value.casefold()
    if "ask before" in folded or folded.startswith("always "):
        return "constraint"
    if "project" in folded:
        return "project"
    if "prefer" in folded or "usual" in folded:
        return "preference"
    return "preference"


def memory_value_from_original(text: str) -> str:
    value = re.sub(r"^\s*remember\s+that\s+", "", text, flags=re.I).strip()
    return re.sub(r"[.?!]+$", "", value).strip()
