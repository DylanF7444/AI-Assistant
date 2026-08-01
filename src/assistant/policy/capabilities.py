from __future__ import annotations

from collections.abc import Iterable

from assistant.contracts import PermissionScope

BUILT_IN_CAPABILITIES = (
    "clipboard.read",
    "clipboard.write",
    "device.control",
    "device.read",
    "filesystem.read",
    "filesystem.write",
    "input.control",
    "integration.read",
    "integration.write",
    "media.control",
    "messaging",
    "network",
    "notification.create",
    "process.launch",
    "process.terminate",
    "process.window",
    "settings.get",
    "settings.set",
    "system.apps",
    "system.display",
    "system.network",
    "system.power",
)


def available_capabilities(
    declared_permissions: Iterable[PermissionScope] = (),
) -> tuple[str, ...]:
    return tuple(
        sorted(set(BUILT_IN_CAPABILITIES) | {scope.capability for scope in declared_permissions})
    )
