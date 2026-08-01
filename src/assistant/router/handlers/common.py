from __future__ import annotations

import re

EXPLICIT_FILE_EXTENSION = re.compile(
    r"\.(?:csv|docx?|gif|jpe?g|json|log|md|mp3|mp4|pdf|png|pptx?|"
    r"py|rtf|svg|text|tsv|txt|wav|webp|xlsx?|xml|zip)$",
    flags=re.IGNORECASE,
)


def has_explicit_file_extension(value: str) -> bool:
    return bool(EXPLICIT_FILE_EXTENSION.search(value.strip()))


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
    value = re.sub(r"^\s*(?:please\s+)?remember\s+(?:that\s+)?", "", text, flags=re.I).strip()
    return re.sub(r"[.?!]+$", "", value).strip()
