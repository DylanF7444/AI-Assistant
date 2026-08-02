from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol

from assistant.context.budget import ContextBudget
from assistant.context.snapshot import ContextItem, ContextSnapshot
from assistant.contracts import RequestResultItem, RiskLevel, SkillManifest
from assistant.policy.file_access import FileAccessPolicyStore
from assistant.policy.permissions import PermissionManager
from assistant.results import result_item_context
from assistant.skills.registry import SkillRegistry
from assistant.storage.memory import MemoryStore
from assistant.windows.paths import KNOWN_USER_FOLDERS, resolve_user_path


class ContextProvider(Protocol):
    name: str

    async def collect(
        self,
        request_text: str,
        *,
        session_id: str,
        budget: ContextBudget,
    ) -> list[ContextItem]: ...


class SessionContextProvider:
    name = "session"

    def __init__(
        self,
        *,
        recent_context: Callable[[], Iterable[dict[str, str]]] | None = None,
        pending_clarification: Callable[[], Mapping[str, str] | None] | None = None,
        selected_result_item: Callable[[], RequestResultItem | None] | None = None,
        result_items: Callable[[], Iterable[RequestResultItem]] | None = None,
        result_source: Callable[[], str | None] | None = None,
        selected_file: Callable[[], str | None] | None = None,
        file_candidates: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self.recent_context = recent_context or (lambda: ())
        self.pending_clarification = pending_clarification or (lambda: None)
        self.selected_result_item = selected_result_item
        self.result_items = result_items
        self.result_source = result_source or (lambda: None)
        self.selected_file = selected_file or (lambda: None)
        self.file_candidates = file_candidates or (lambda: ())

    async def collect(
        self,
        request_text: str,
        *,
        session_id: str,
        budget: ContextBudget,
    ) -> list[ContextItem]:
        items: list[ContextItem] = []
        recent = list(self.recent_context())
        if recent:
            items.append(
                ContextItem(
                    key="session.recent_interactions",
                    title="Recent interactions",
                    value=recent,
                    sensitivity="local",
                    source=self.name,
                    summary=f"{len(recent)} recent interactions.",
                )
            )
        pending = self.pending_clarification()
        if pending:
            items.append(
                ContextItem(
                    key="session.pending_clarification",
                    title="Pending clarification",
                    value=dict(pending),
                    sensitivity="local",
                    source=self.name,
                    summary="A clarification is waiting for the user's answer.",
                )
            )
        candidates = list(self.file_candidates())
        generic_items = list(self.result_items()) if self.result_items is not None else []
        if not generic_items and candidates:
            generic_items = [
                RequestResultItem(
                    item_id=f"file-{index}",
                    title=path,
                    value=path,
                    kind="file",
                    metadata={"path": path},
                )
                for index, path in enumerate(candidates, 1)
            ]
        selected = self.selected_file()
        selected_item = (
            self.selected_result_item()
            if self.selected_result_item is not None
            else next(
                (
                    item
                    for item in generic_items
                    if item.kind == "file" and item.value == selected
                ),
                None,
            )
        )
        source = self.result_source()
        if selected_item is not None:
            try:
                position = generic_items.index(selected_item) + 1
            except ValueError:
                position = 1
            items.append(
                ContextItem(
                    key="results.selected",
                    title="Selected request result",
                    value=_selected_result_context(
                        selected_item,
                        position=position,
                        source=source,
                        budget=budget,
                    ),
                    sensitivity="local",
                    source=self.name,
                    summary=(
                        f"Selected {selected_item.kind} result {position} is available "
                        "for a follow-up request."
                    ),
                )
            )
        if generic_items:
            items.append(
                ContextItem(
                    key="results.recent_items",
                    title="Recent request result items",
                    value=_recent_result_context(
                        generic_items,
                        source=source,
                        budget=budget,
                    ),
                    sensitivity="local",
                    source=self.name,
                    summary=(
                        f"{len(generic_items)} recent request result item"
                        f"{'s' if len(generic_items) != 1 else ''} can be referenced "
                        "by number, ID, or title."
                    ),
                )
            )
        if selected:
            items.append(
                ContextItem(
                    key="files.selected",
                    title="Selected file",
                    value=selected,
                    sensitivity="local",
                    source=self.name,
                    summary="Current selected file from recent file results.",
                )
            )
        if candidates:
            items.append(
                ContextItem(
                    key="files.recent_candidates",
                    title="Recent file candidates",
                    value=candidates,
                    sensitivity="local",
                    source=self.name,
                    summary=f"{len(candidates)} recent file candidates.",
                )
            )
        return items


def _selected_result_context(
    item: RequestResultItem,
    *,
    position: int,
    source: str | None,
    budget: ContextBudget,
) -> dict[str, object]:
    value = {"source": source, **result_item_context(item, position=position)}
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        value["metadata"] = {
            key: metadata_value
            for key, metadata_value in metadata.items()
            if metadata_value != value.get("value")
        }
    description = value.get("description")
    if isinstance(description, str):
        value["description"] = description[:300]
    if budget.value_size(value) <= budget.max_item_value_chars:
        return value
    value.pop("description", None)
    if budget.value_size(value) <= budget.max_item_value_chars:
        return value
    value.pop("metadata", None)
    return value


def _recent_result_context(
    items: list[RequestResultItem],
    *,
    source: str | None,
    budget: ContextBudget,
) -> dict[str, object]:
    title_limit = 140

    def compact(limit: int) -> dict[str, object]:
        return {
            "source": source,
            "items": [
                {
                    "position": index,
                    "reference": f"item {index}",
                    "item_id": item.item_id[:80],
                    "kind": item.kind[:50],
                    "title": (
                        "[sensitive item]" if item.sensitive else item.title[:limit]
                    ),
                }
                for index, item in enumerate(items[:20], 1)
            ],
        }

    value = compact(title_limit)
    while budget.value_size(value) > budget.max_item_value_chars and title_limit > 30:
        title_limit -= 10
        value = compact(title_limit)
    if budget.value_size(value) <= budget.max_item_value_chars:
        return value
    for entry in value["items"]:
        entry.pop("kind", None)
        entry.pop("reference", None)
        entry["item_id"] = str(entry["item_id"])[:40]
    return value


class SkillContextProvider:
    name = "skills"

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    async def collect(
        self,
        request_text: str,
        *,
        session_id: str,
        budget: ContextBudget,
    ) -> list[ContextItem]:
        manifests = tuple(
            manifest
            for manifest in self.registry.list()
            if self.registry.is_enabled(manifest.skill_id)
        )
        return [
            ContextItem(
                key="skills.available",
                title="Available skills",
                value=[
                    {
                        "id": manifest.skill_id,
                        "name": manifest.display_name,
                        "description": manifest.description,
                        "risk": manifest.risk.value,
                        "enabled": self.registry.is_enabled(manifest.skill_id),
                    }
                    for manifest in manifests
                ],
                sensitivity="local",
                source=self.name,
                summary=f"{len(manifests)} enabled skills.",
            ),
            ContextItem(
                key="skills.risk_summary",
                title="Skill risk summary",
                value=_risk_summary(manifests),
                sensitivity="local",
                source=self.name,
                summary="Enabled skills grouped by risk level.",
            ),
            ContextItem(
                key="skills.permission_summary",
                title="Skill permission summary",
                value=_permission_summary(manifests),
                sensitivity="local",
                source=self.name,
                summary="Enabled skill IDs grouped by declared permission capability.",
            ),
        ]


class PermissionContextProvider:
    name = "permissions"

    def __init__(
        self,
        permissions: PermissionManager,
        *,
        file_policy_store: FileAccessPolicyStore | None = None,
        confirmation_overrides: Mapping[str, bool] | None = None,
    ) -> None:
        self.permissions = permissions
        self.file_policy_store = file_policy_store
        self.confirmation_overrides = dict(confirmation_overrides or {})

    async def collect(
        self,
        request_text: str,
        *,
        session_id: str,
        budget: ContextBudget,
    ) -> list[ContextItem]:
        return [
            ContextItem(
                key="permissions.granted",
                title="Granted permissions",
                value=self._grant_summary(),
                sensitivity="local",
                source=self.name,
                summary="Current stored permission grants and denials.",
            ),
            ContextItem(
                key="permissions.file_policy",
                title="File access policy",
                value=self._file_policy_summary(),
                sensitivity="local",
                source=self.name,
                summary="Configured file access policy rules.",
            ),
            ContextItem(
                key="permissions.confirmation_required",
                title="Confirmation settings",
                value=dict(sorted(self.confirmation_overrides.items())),
                sensitivity="local",
                source=self.name,
                summary="Configured confirmation prompt overrides.",
            ),
        ]

    def _grant_summary(self) -> list[dict[str, object]]:
        return [
            {
                "skill_id": grant["skill_id"],
                "capability": grant["capability"],
                "effect": grant["effect"],
                "grant_type": grant["grant_type"],
                "targets": grant["targets"],
                "session_scoped": bool(grant["session_id"]),
                "expires_at": (
                    grant["expires_at"].isoformat() if grant["expires_at"] is not None else None
                ),
            }
            for grant in self.permissions.list_grants()
        ]

    def _file_policy_summary(self) -> dict[str, list[str]]:
        store = self.file_policy_store or self.permissions.file_policy_store
        if store is None:
            return {
                "read_whitelist": [],
                "read_blacklist": [],
                "write_whitelist": [],
                "write_blacklist": [],
            }
        return store.get().to_mapping()


class PreferenceContextProvider:
    name = "memory"

    GROUP_KEYS = {
        "preference": "memory.preferences",
        "constraint": "memory.constraints",
        "project": "memory.projects",
        "workflow": "memory.workflows",
        "identity": "memory.identity",
        "correction": "memory.corrections",
    }

    def __init__(self, memory_store: MemoryStore) -> None:
        self.memory_store = memory_store

    async def collect(
        self,
        request_text: str,
        *,
        session_id: str,
        budget: ContextBudget,
    ) -> list[ContextItem]:
        grouped: dict[str, list[dict[str, object]]] = {}
        for item in self.memory_store.list():
            grouped.setdefault(item.type, []).append(
                {
                    "id": item.id,
                    "type": item.type,
                    "key": item.key,
                    "value": item.value,
                    "confidence": item.confidence,
                }
            )
        context_items = []
        for memory_type, values in sorted(grouped.items()):
            key = self.GROUP_KEYS.get(memory_type, f"memory.{memory_type}")
            context_items.append(
                ContextItem(
                    key=key,
                    title=f"{memory_type.replace('_', ' ').title()} memories",
                    value=values,
                    sensitivity="local",
                    source=self.name,
                    summary=f"{len(values)} enabled {memory_type} memories.",
                )
            )
        return context_items


class DesktopContextProvider:
    name = "desktop"
    RELEVANT_SKILLS = frozenset(
        {
            "windows.application_window",
            "windows.browser_navigate",
            "windows.context_navigation",
            "windows.input_automation",
            "windows.launcher",
        }
    )
    CONTEXT_WORDS = re.compile(
        r"\b(?:active|app|application|browser|desktop|focus|foreground|monitor|"
        r"screen|tab|window|this|that)\b",
        flags=re.IGNORECASE,
    )

    def __init__(
        self,
        adapter,
        *,
        include_open_applications: bool = True,
        include_window_titles: bool = False,
    ) -> None:
        self.adapter = adapter
        self.include_open_applications = include_open_applications
        self.include_window_titles = include_window_titles

    def is_relevant(
        self,
        request_text: str,
        candidate_skill_ids: tuple[str, ...],
    ) -> bool:
        return bool(
            self.CONTEXT_WORDS.search(request_text)
            or self.RELEVANT_SKILLS.intersection(candidate_skill_ids)
        )

    async def collect(
        self,
        request_text: str,
        *,
        session_id: str,
        budget: ContextBudget,
    ) -> list[ContextItem]:
        if not self.include_open_applications:
            return []
        items: list[ContextItem] = []
        active = await _maybe_await(self.adapter, "active_process_name")
        if active:
            items.append(
                ContextItem(
                    key="desktop.focused_application",
                    title="Focused application",
                    value={"process_name": active},
                    sensitivity="local",
                    source=self.name,
                    summary=f"Focused application process: {active}.",
                )
            )
        visible = await _maybe_await(self.adapter, "visible_windows")
        if visible:
            items.append(
                ContextItem(
                    key="desktop.visible_windows",
                    title="Visible windows",
                    value=[
                        _window_summary(
                            window,
                            include_title=self.include_window_titles,
                        )
                        for window in visible
                    ][:12],
                    sensitivity="local",
                    source=self.name,
                    summary=f"{min(len(visible), 12)} visible windows summarized.",
                )
            )
        monitor_layout = _monitor_layout(self.adapter)
        if monitor_layout:
            items.append(
                ContextItem(
                    key="desktop.monitor_layout",
                    title="Monitor layout",
                    value=monitor_layout,
                    sensitivity="local",
                    source=self.name,
                    summary=f"{len(monitor_layout)} monitor work areas available.",
                )
            )
        return items


class ProcessContextProvider:
    name = "processes"
    RELEVANT_SKILLS = frozenset(
        {
            "windows.application_window",
            "windows.context_navigation",
            "windows.launcher",
            "windows.process",
        }
    )
    CONTEXT_WORDS = re.compile(
        r"\b(?:app|application|close|focus|launch|minimize|maximize|process|"
        r"program|running|start|window)\b",
        flags=re.IGNORECASE,
    )

    BACKGROUND_NAMES = {
        "conhost.exe",
        "csrss.exe",
        "dwm.exe",
        "fontdrvhost.exe",
        "lsass.exe",
        "registry",
        "services.exe",
        "sihost.exe",
        "smss.exe",
        "svchost.exe",
        "system",
        "wininit.exe",
        "winlogon.exe",
    }

    def __init__(
        self,
        adapter,
        *,
        include_open_applications: bool = True,
        cache_ttl_seconds: float = 2.0,
    ) -> None:
        self.adapter = adapter
        self.include_open_applications = include_open_applications
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache_lock = threading.Lock()
        self._cache_timestamp = 0.0
        self._cache_items: tuple[ContextItem, ...] = ()

    def is_relevant(
        self,
        request_text: str,
        candidate_skill_ids: tuple[str, ...],
    ) -> bool:
        return bool(
            self.CONTEXT_WORDS.search(request_text)
            or self.RELEVANT_SKILLS.intersection(candidate_skill_ids)
        )

    def _cached(self) -> list[ContextItem] | None:
        with self._cache_lock:
            if (
                self._cache_items
                and time.monotonic() - self._cache_timestamp <= self.cache_ttl_seconds
            ):
                return list(self._cache_items)
        return None

    def _remember(self, items: list[ContextItem]) -> None:
        with self._cache_lock:
            self._cache_items = tuple(items)
            self._cache_timestamp = time.monotonic()

    async def collect(
        self,
        request_text: str,
        *,
        session_id: str,
        budget: ContextBudget,
    ) -> list[ContextItem]:
        if not self.include_open_applications:
            return []
        if cached := self._cached():
            return cached
        processes = await self.adapter.list()
        filtered = [
            process
            for process in processes
            if process.image_name.casefold() not in self.BACKGROUND_NAMES
        ]
        items = [
            ContextItem(
                key="processes.user_apps",
                title="User application processes",
                value=[_process_summary(process) for process in filtered[:20]],
                sensitivity="local",
                source=self.name,
                summary=f"{min(len(filtered), 20)} likely user processes summarized.",
            )
        ]
        high_resource = sorted(
            filtered,
            key=lambda process: _memory_kb(process.memory),
            reverse=True,
        )[:5]
        if high_resource:
            items.append(
                ContextItem(
                    key="processes.high_resource",
                    title="High resource processes",
                    value=[_process_summary(process) for process in high_resource],
                    sensitivity="local",
                    source=self.name,
                    summary="Highest-memory user processes.",
                )
            )
        self._remember(items)
        return items


class DeviceContextProvider:
    name = "devices"
    RELEVANT_SKILLS = frozenset(
        {
            "windows.device_control",
            "windows.setting",
        }
    )
    CONTEXT_WORDS = re.compile(
        r"\b(?:battery|bluetooth|brightness|cpu|device|disk|display|drive|"
        r"headset|memory|microphone|performance|power|speaker|storage|volume|"
        r"wifi|wi-fi)\b",
        flags=re.IGNORECASE,
    )

    def __init__(
        self,
        adapter,
        *,
        include_connected_devices: bool = True,
        cache_ttl_seconds: float = 2.0,
    ) -> None:
        self.adapter = adapter
        self.include_connected_devices = include_connected_devices
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache_lock = threading.Lock()
        self._cache_timestamp = 0.0
        self._cache_items: tuple[ContextItem, ...] = ()

    def is_relevant(
        self,
        request_text: str,
        candidate_skill_ids: tuple[str, ...],
    ) -> bool:
        return bool(
            self.CONTEXT_WORDS.search(request_text)
            or self.RELEVANT_SKILLS.intersection(candidate_skill_ids)
        )

    def _cached(self) -> list[ContextItem] | None:
        with self._cache_lock:
            if (
                self._cache_items
                and time.monotonic() - self._cache_timestamp <= self.cache_ttl_seconds
            ):
                return list(self._cache_items)
        return None

    def _remember(self, items: list[ContextItem]) -> None:
        with self._cache_lock:
            self._cache_items = tuple(items)
            self._cache_timestamp = time.monotonic()

    async def collect(
        self,
        request_text: str,
        *,
        session_id: str,
        budget: ContextBudget,
    ) -> list[ContextItem]:
        if not self.include_connected_devices:
            return []
        if cached := self._cached():
            return cached
        items: list[ContextItem] = []
        battery, performance, connected, storage = await asyncio.gather(
            _safe_adapter_call(self.adapter.battery_status),
            _safe_adapter_call(self.adapter.performance_status),
            _safe_adapter_call(self.adapter.connected_devices, limit=20),
            _safe_adapter_call(self.adapter.storage_status),
        )
        if battery:
            items.append(
                ContextItem(
                    key="devices.battery",
                    title="Battery",
                    value=battery,
                    sensitivity="local",
                    source=self.name,
                    summary="Battery status is available.",
                )
            )
        if performance:
            items.append(
                ContextItem(
                    key="devices.performance",
                    title="Device performance",
                    value=performance,
                    sensitivity="local",
                    source=self.name,
                    summary="CPU and memory status are available.",
                )
            )
        if connected:
            items.append(
                ContextItem(
                    key="devices.connected",
                    title="Connected devices",
                    value=[_connected_device_summary(item) for item in connected],
                    sensitivity="local",
                    source=self.name,
                    summary=f"{len(connected)} connected devices summarized.",
                )
            )
        if storage:
            items.append(
                ContextItem(
                    key="devices.storage",
                    title="Storage",
                    value=storage,
                    sensitivity="local",
                    source=self.name,
                    summary="Storage status is available.",
                )
            )
        self._remember(items)
        return items


class FileContextProvider:
    name = "files"

    FILE_WORDS = re.compile(
        r"\b(file|folder|directory|downloads?|documents?|desktop|pictures?|"
        r"music|videos?|organize|clean up|move|copy|delete|find)\b",
        flags=re.IGNORECASE,
    )

    def __init__(
        self,
        adapter,
        *,
        file_policy_store: FileAccessPolicyStore | None = None,
        allow_file_contents: bool = False,
        known_folders: Mapping[str, str] = KNOWN_USER_FOLDERS,
    ) -> None:
        self.adapter = adapter
        self.file_policy_store = file_policy_store
        self.allow_file_contents = allow_file_contents
        self.known_folders = dict(known_folders)

    def is_relevant(
        self,
        request_text: str,
        candidate_skill_ids: tuple[str, ...],
    ) -> bool:
        return bool(
            self.FILE_WORDS.search(request_text)
            or {
                "media.present",
                "windows.file_operation",
                "windows.file_search",
            }.intersection(candidate_skill_ids)
        )

    async def collect(
        self,
        request_text: str,
        *,
        session_id: str,
        budget: ContextBudget,
    ) -> list[ContextItem]:
        if not self.FILE_WORDS.search(request_text):
            return []
        folders = _unique_known_folders(self.known_folders)
        folder_values = []
        summaries = []
        policy = self.file_policy_store.get() if self.file_policy_store else None
        for name, path in folders.items():
            denied = policy.denial_reason("filesystem.read", str(path)) if policy else None
            value = {
                "name": name,
                "path": str(path),
                "exists": path.exists(),
                "read_allowed": denied is None,
            }
            if denied:
                value["policy"] = "blocked"
            folder_values.append(value)
            if denied is None and path.is_dir():
                summaries.append(_folder_summary(name, path))
        items = [
            ContextItem(
                key="files.known_folders",
                title="Known user folders",
                value=folder_values,
                sensitivity="local",
                source=self.name,
                summary=f"{len(folder_values)} known user folders summarized.",
            )
        ]
        if summaries:
            items.append(
                ContextItem(
                    key="files.folder_summaries",
                    title="Folder summaries",
                    value=summaries,
                    sensitivity="local",
                    source=self.name,
                    summary="Folder counts and extensions; file contents were not read.",
                )
            )
        return items


async def collect_context_snapshot(
    request_text: str,
    *,
    session_id: str,
    providers: tuple[ContextProvider, ...] = (),
    budget: ContextBudget | None = None,
    candidate_skill_ids: tuple[str, ...] | None = None,
) -> ContextSnapshot:
    budget = budget or ContextBudget()
    items: list[ContextItem] = []
    total_value_chars = 0

    async def collect_provider(provider: ContextProvider):
        if candidate_skill_ids is not None:
            relevance = getattr(provider, "is_relevant", None)
            if callable(relevance) and not relevance(request_text, candidate_skill_ids):
                return []
        try:
            return await provider.collect(
                request_text,
                session_id=session_id,
                budget=budget,
            )
        except Exception as exc:
            return exc

    results = await asyncio.gather(*(collect_provider(provider) for provider in providers))
    for provider, result in zip(providers, results, strict=True):
        if isinstance(result, Exception):
            if not budget.include_provider_errors:
                continue
            provided = [
                ContextItem(
                    key=f"context.provider_error.{provider.name}",
                    title=f"{provider.name} context unavailable",
                    value={
                        "provider": provider.name,
                        "error": result.__class__.__name__,
                    },
                    sensitivity="public",
                    source="context",
                    summary="A context provider failed while collecting context.",
                )
            ]
        else:
            provided = result
        for item in provided:
            if item.sensitivity == "sensitive" and not budget.include_sensitive:
                continue
            value_size = budget.value_size(item.value)
            if (
                value_size > budget.max_item_value_chars
                and item.key != "results.selected"
            ):
                item = item.model_copy(
                    update={
                        "value": str(item.value)[: budget.max_item_value_chars],
                        "summary": item.summary or "Value truncated to fit context budget.",
                    }
                )
                value_size = budget.value_size(item.value)
            if total_value_chars + value_size > budget.max_total_value_chars:
                break
            items.append(item)
            total_value_chars += value_size
            if len(items) >= budget.max_items:
                break
        if len(items) >= budget.max_items:
            break
    return ContextSnapshot(
        request_text=request_text,
        session_id=session_id,
        items=items,
    )


def _risk_summary(manifests: tuple[SkillManifest, ...]) -> dict[str, list[str]]:
    summary = {risk.value: [] for risk in RiskLevel}
    for manifest in manifests:
        summary[manifest.risk.value].append(manifest.skill_id)
    return {risk: skill_ids for risk, skill_ids in summary.items() if skill_ids}


def _permission_summary(manifests: tuple[SkillManifest, ...]) -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {}
    for manifest in manifests:
        for permission in manifest.permissions:
            summary.setdefault(permission.capability, []).append(manifest.skill_id)
    return {key: sorted(value) for key, value in sorted(summary.items())}


async def _maybe_await(adapter, method_name: str):
    method = getattr(adapter, method_name, None)
    if method is None:
        return None
    return await method()


async def _safe_adapter_call(method, *args, **kwargs):
    try:
        return await method(*args, **kwargs)
    except Exception:
        return None


def _window_summary(window, *, include_title: bool) -> dict[str, object]:
    if isinstance(window, Mapping):
        process_name = window.get("process_name") or window.get("image_name")
        title = window.get("title")
        private = bool(window.get("private", False))
    else:
        process_name = getattr(window, "process_name", None) or getattr(
            window,
            "image_name",
            None,
        )
        title = getattr(window, "title", None)
        private = bool(getattr(window, "private", False))
    value: dict[str, object] = {"process_name": process_name, "private": private}
    if include_title and title and not private:
        value["title"] = title
    elif title:
        value["title"] = "[omitted]"
    return value


def _monitor_layout(adapter) -> list[dict[str, int]]:
    method = getattr(adapter, "_monitor_work_areas", None)
    if method is None:
        return []
    try:
        areas = method()
    except Exception:
        return []
    return [
        {"monitor": index, "left": left, "top": top, "right": right, "bottom": bottom}
        for index, (left, top, right, bottom) in enumerate(areas, 1)
    ]


def _process_summary(process) -> dict[str, object]:
    return {
        "image_name": process.image_name,
        "pid": process.pid,
        "memory": process.memory,
    }


def _memory_kb(value: str) -> int:
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else 0


def _connected_device_summary(item: Mapping) -> dict[str, object]:
    return {
        key: item[key]
        for key in ("FriendlyName", "Name", "Class", "Status")
        if key in item and item[key]
    }


def _unique_known_folders(folders: Mapping[str, str]) -> dict[str, Path]:
    values: dict[str, Path] = {}
    for _name, folder in folders.items():
        canonical = folder.casefold()
        if canonical in values:
            continue
        try:
            raw_path = Path(folder).expanduser()
            values[canonical] = (
                raw_path.resolve() if raw_path.is_absolute() else resolve_user_path(folder)
            )
        except ValueError:
            continue
    return values


def _folder_summary(name: str, path: Path) -> dict[str, object]:
    extensions: dict[str, int] = {}
    total = 0
    oldest = None
    newest = None
    try:
        children = list(path.iterdir())[:500]
    except OSError:
        children = []
    for child in children:
        total += 1
        if child.is_file():
            extension = child.suffix.casefold() or "[none]"
            extensions[extension] = extensions.get(extension, 0) + 1
        try:
            modified = child.stat().st_mtime
        except OSError:
            continue
        oldest = modified if oldest is None else min(oldest, modified)
        newest = modified if newest is None else max(newest, modified)
    return {
        "name": name,
        "path": str(path),
        "entries_sampled": total,
        "extensions": dict(sorted(extensions.items())),
        "oldest_modified": oldest,
        "newest_modified": newest,
        "contents_read": False,
    }
