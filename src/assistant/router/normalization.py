from __future__ import annotations

import re

_NUMBERS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def spoken_number(text: str) -> float:
    text = text.strip().casefold()
    try:
        return float(text)
    except ValueError:
        pass
    total = current = 0
    for word in re.split(r"[\s-]+", text):
        if word in _NUMBERS:
            current += _NUMBERS[word]
        elif word == "hundred":
            current = max(current, 1) * 100
        elif word == "thousand":
            total += max(current, 1) * 1000
            current = 0
        elif word in {"and", "a", "an"}:
            continue
        else:
            raise ValueError(f"Not a spoken number: {text}")
    return float(total + current)


def normalize_text(text: str, aliases: dict[str, str] | None = None) -> str:
    normalized = " ".join(text.strip().split()).casefold()
    punctuation = {
        " comma": ",",
        " period": ".",
        " question mark": "?",
        " exclamation mark": "!",
    }
    for spoken, symbol in punctuation.items():
        normalized = normalized.replace(spoken, symbol)
    for spoken, canonical in sorted((aliases or {}).items(), key=lambda item: -len(item[0])):
        normalized = re.sub(rf"\b{re.escape(spoken.casefold())}\b", canonical, normalized)
    return normalized


def command_text(text: str) -> str:
    """Remove sentence-ending punctuation that should not change command routing."""
    return re.sub(r"[.?!]+$", "", normalize_text(text)).rstrip()
