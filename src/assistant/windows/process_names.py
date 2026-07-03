from __future__ import annotations

import re

ASSISTANT_PROCESS_NAME = "LocalAssistant.exe"
ASSISTANT_WINDOW_TITLE = "Local Assistant"
ASSISTANT_DEVELOPMENT_PROCESS_NAMES = ("python.exe", "pythonw.exe")

PROCESS_NAME_ALIASES = {
    "assistant.exe": ASSISTANT_PROCESS_NAME,
    "edge.exe": "msedge.exe",
    "google chrome.exe": "chrome.exe",
    "googlechrome.exe": "chrome.exe",
    "librewolf browser.exe": "librewolf.exe",
    "librewolfbrowser.exe": "librewolf.exe",
    "microsoft edge.exe": "msedge.exe",
    "microsoftedge.exe": "msedge.exe",
    "mozilla firefox.exe": "firefox.exe",
    "mozillafirefox.exe": "firefox.exe",
    "opera browser.exe": "opera.exe",
    "operabrowser.exe": "opera.exe",
    "opera gx.exe": "opera_gx.exe",
    "operagx.exe": "opera_gx.exe",
    "vivaldi browser.exe": "vivaldi.exe",
    "vivaldibrowser.exe": "vivaldi.exe",
    "localassistant.exe": ASSISTANT_PROCESS_NAME,
    "roblox.exe": "RobloxPlayerBeta.exe",
    "waterfox browser.exe": "waterfox.exe",
    "waterfoxbrowser.exe": "waterfox.exe",
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


def is_assistant_process_name(value: str) -> bool:
    return (
        normalize_process_name(value).casefold()
        == ASSISTANT_PROCESS_NAME.casefold()
    )
