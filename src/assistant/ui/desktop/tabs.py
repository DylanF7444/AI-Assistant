from __future__ import annotations

TAB_LABELS = (
    "Dashboard",
    "Assistant",
    "Automations",
    "Skills",
    "Extension Hub",
    "Permissions",
    "File Access",
    "Audit Log",
    "Settings",
    "Diagnostics",
    "Macros",
    "Playbooks",
    "Models",
    "Knowledge",
    "Memories",
    "Privacy",
    "Applications",
)

TAB_GROUPS = {
    "main": ("Dashboard", "Assistant"),
    "assistant": ("Automations",),
    "extensions": ("Skills", "Extension Hub", "Macros", "Playbooks"),
    "security": ("Permissions", "File Access", "Audit Log", "Privacy"),
    "knowledge": ("Knowledge", "Memories"),
    "configuration": ("Settings", "Models", "Applications"),
    "diagnostics": ("Diagnostics",),
}
