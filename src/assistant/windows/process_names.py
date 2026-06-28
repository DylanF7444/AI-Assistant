from __future__ import annotations

import re

PROCESS_NAME_ALIASES = {
    "roblox.exe": "RobloxPlayerBeta.exe",
}


def normalize_process_name(value: str) -> str:
    process_name = value.strip()
    match = re.fullmatch(
        r"(?P<name>[^\\/:*?\"<>|]+\.exe)[^\w\\/:*?\"<>|.]+",
        process_name,
        re.IGNORECASE,
    )
    if match:
        process_name = match.group("name")
    return PROCESS_NAME_ALIASES.get(process_name.casefold(), process_name)


def executable_from_command_name(value: str) -> str:
    process_name = value.strip()
    if not process_name.casefold().endswith(".exe"):
        process_name = f"{process_name}.exe"
    return normalize_process_name(process_name)
