from __future__ import annotations

TAB_LABELS = (
    "Assistant",
    "Active Scripts",
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
    "assistant": ("Assistant", "Active Scripts"),
    "extensions": ("Skills", "Macros"),
    "security": ("Permissions", "File Access", "Audit Log", "Privacy"),
    "knowledge": ("Knowledge", "Memories"),
    "configuration": ("Settings", "Models", "Applications"),
    "diagnostics": ("Diagnostics",),
}
