from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from assistant.contracts import BackgroundMode, PermissionScope, SkillManifest

# These capabilities either target the user's interactive desktop or can visibly
# change the machine they are actively using. They must never run unattended in
# the same Windows session.
FOREGROUND_CAPABILITIES = frozenset(
    {
        "clipboard.read",
        "clipboard.write",
        "device.control",
        "input.control",
        "media.control",
        "process.launch",
        "process.terminate",
        "process.window",
        "settings.set",
        "system.display",
        "system.network",
        "system.power",
        "window.control",
    }
)


@dataclass(frozen=True)
class BackgroundCompatibility:
    allowed: bool
    mode: BackgroundMode
    blocking_capabilities: tuple[str, ...] = ()
    reason: str = ""


def background_compatibility(
    manifest: SkillManifest,
    permissions: Iterable[PermissionScope],
) -> BackgroundCompatibility:
    if manifest.background_mode == BackgroundMode.FOREGROUND_ONLY:
        return BackgroundCompatibility(
            allowed=False,
            mode=manifest.background_mode,
            reason="The skill is restricted to foreground execution.",
        )
    if manifest.background_mode == BackgroundMode.SAFE:
        return BackgroundCompatibility(allowed=True, mode=manifest.background_mode)

    blocking = tuple(
        sorted(
            {
                permission.capability
                for permission in permissions
                if permission.capability in FOREGROUND_CAPABILITIES
            }
        )
    )
    if blocking:
        return BackgroundCompatibility(
            allowed=False,
            mode=manifest.background_mode,
            blocking_capabilities=blocking,
            reason=(
                "Background execution would affect the user's active desktop or device: "
                + ", ".join(blocking)
            ),
        )
    return BackgroundCompatibility(allowed=True, mode=manifest.background_mode)


def background_mode_summary(manifest: SkillManifest) -> str:
    if manifest.background_mode == BackgroundMode.SAFE:
        return "Background safe — designed to run without using the interactive desktop."
    if manifest.background_mode == BackgroundMode.FOREGROUND_ONLY:
        return "Foreground only — may use or visibly affect the interactive desktop."
    return (
        "Background eligible — allowed only when the exact action requests no "
        "foreground-only capability."
    )
