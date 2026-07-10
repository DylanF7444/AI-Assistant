from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from assistant.storage.database import SettingsStore
from assistant.windows.paths import resolve_user_path

POLICY_SETTINGS_KEY = "file_access_policy"


@dataclass(frozen=True)
class FileAccessPolicy:
    read_whitelist: tuple[str, ...] = ()
    read_blacklist: tuple[str, ...] = ()
    write_whitelist: tuple[str, ...] = ()
    write_blacklist: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: dict | None) -> FileAccessPolicy:
        value = value or {}
        return cls(
            read_whitelist=_clean_paths(value.get("read_whitelist", ())),
            read_blacklist=_clean_paths(value.get("read_blacklist", ())),
            write_whitelist=_clean_paths(value.get("write_whitelist", ())),
            write_blacklist=_clean_paths(value.get("write_blacklist", ())),
        )

    def to_mapping(self) -> dict[str, list[str]]:
        return {
            "read_whitelist": list(self.read_whitelist),
            "read_blacklist": list(self.read_blacklist),
            "write_whitelist": list(self.write_whitelist),
            "write_blacklist": list(self.write_blacklist),
        }

    def denial_reason(self, capability: str, target: str) -> str | None:
        if capability == "filesystem.read":
            whitelist = self.read_whitelist
            blacklist = self.read_blacklist
            access = "read"
        elif capability == "filesystem.write":
            whitelist = self.write_whitelist
            blacklist = self.write_blacklist
            access = "write/delete"
        else:
            return None
        if any(_target_in_rule(target, rule) for rule in blacklist):
            return f"File access policy blocks {access} access to {target}."
        if whitelist and not any(_target_in_rule(target, rule) for rule in whitelist):
            return (
                f"File access policy allows {access} access only inside the configured whitelist."
            )
        return None


class FileAccessPolicyStore:
    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings

    def get(self) -> FileAccessPolicy:
        return FileAccessPolicy.from_mapping(self.settings.get(POLICY_SETTINGS_KEY, {}))

    def set(self, policy: FileAccessPolicy) -> None:
        self.settings.set(POLICY_SETTINGS_KEY, policy.to_mapping())

    def update_entry(
        self,
        *,
        list_name: str,
        access: str,
        path: str,
        add: bool,
    ) -> FileAccessPolicy:
        policy = self.get()
        field = _field_name(list_name, access)
        current = list(getattr(policy, field))
        normalized = _normalize_policy_entry(path)
        if add and normalized not in current:
            current.append(normalized)
        if not add:
            current = [item for item in current if item != normalized]
        updated = FileAccessPolicy.from_mapping({**policy.to_mapping(), field: current})
        self.set(updated)
        return updated


def _field_name(list_name: str, access: str) -> str:
    list_name = list_name.casefold().strip()
    access = access.casefold().strip()
    if list_name not in {"whitelist", "blacklist"}:
        raise ValueError("list_name must be whitelist or blacklist")
    if access in {"write", "delete", "write/delete", "writing", "deleting"}:
        access = "write"
    elif access in {"read", "reading"}:
        access = "read"
    else:
        raise ValueError("access must be read or write")
    return f"{access}_{list_name}"


def _clean_paths(values) -> tuple[str, ...]:
    cleaned = []
    for value in values or ():
        text = str(value).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return tuple(cleaned)


def _normalize_policy_entry(value: str) -> str:
    text = str(value).strip()
    if text == "*":
        return text
    return str(resolve_user_path(text))


def _target_in_rule(target: str, rule: str) -> bool:
    if rule == "*":
        return True
    try:
        target_path = resolve_user_path(target)
        rule_path = resolve_user_path(rule)
        return target_path == rule_path or rule_path in target_path.parents
    except (OSError, ValueError):
        try:
            target_path = Path(target).resolve()
            rule_path = Path(rule).resolve()
            return target_path == rule_path or rule_path in target_path.parents
        except (OSError, ValueError):
            return target == rule
