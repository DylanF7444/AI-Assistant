from __future__ import annotations

TAB_LABELS = (
    "Assistant",
    "Automations",
    "Skills",
    "Permissions",
    "File Access",
    "Audit Log",
    "Settings",
    "Diagnostics",
    "Macros",
    "Models",
    "Knowledge",
    "Memories",
    "Privacy",
    "Applications",
)

TAB_GROUPS = {
    "assistant": ("Assistant", "Automations"),
    "extensions": ("Skills", "Macros"),
    "security": ("Permissions", "File Access", "Audit Log", "Privacy"),
    "knowledge": ("Knowledge", "Memories"),
    "configuration": ("Settings", "Models", "Applications"),
    "diagnostics": ("Diagnostics",),
}
