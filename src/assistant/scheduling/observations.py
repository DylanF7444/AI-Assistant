from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from assistant.contracts import PermissionScope
from assistant.scheduling.contracts import EventObservation, EventTrigger
from assistant.windows.paths import resolve_user_path


def observation_capability(observation_type: str) -> str:
    return f"automation.observe.{observation_type}"


def trigger_permissions(trigger: EventTrigger) -> tuple[PermissionScope, ...]:
    base = {
        "file": "filesystem.read",
        "calendar": "integration.read",
        "process": "system.apps",
        "device": "device.read",
        "web": "network",
    }[trigger.observation_type]
    return (
        PermissionScope(
            capability=base,
            targets=(trigger.target,),
            reason=f"Observe the exact {trigger.observation_type} trigger target.",
        ),
        PermissionScope(
            capability=observation_capability(trigger.observation_type),
            targets=(trigger.target,),
            reason=f"Keep a typed {trigger.observation_type} event monitor active.",
        ),
    )


def matches(trigger: EventTrigger, observation: EventObservation) -> bool:
    if trigger.observation_type != observation.observation_type:
        return False
    if observation.event not in trigger.events:
        return False
    if trigger.observation_type == "file":
        return _file_target_matches(trigger.target, observation.target)
    if trigger.observation_type == "web":
        return _safe_web_target(trigger.target) == _safe_web_target(observation.target)
    return trigger.target.casefold().strip() == observation.target.casefold().strip()


def _file_target_matches(root: str, target: str) -> bool:
    try:
        root_path = resolve_user_path(root).resolve()
        target_path = resolve_user_path(target).resolve()
    except (OSError, ValueError):
        return False
    return target_path == root_path or root_path in target_path.parents


def _safe_web_target(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return urlunsplit(("https", parsed.hostname.casefold(), parsed.path or "/", "", ""))
