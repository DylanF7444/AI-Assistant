from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

from assistant.integrations.web import WebClient
from assistant.policy.file_access import FileAccessPolicyStore
from assistant.scheduling.contracts import (
    DeviceObservation,
    EventTrigger,
    FileObservation,
    ProcessObservation,
    ScheduleStatus,
    WebObservation,
)
from assistant.scheduling.store import ScheduleStore, aware_utc
from assistant.windows.interfaces import ProcessAdapter, WindowsDeviceAdapter
from assistant.windows.paths import resolve_user_path


class WebFetcher(Protocol):
    async def fetch(self, url: str, max_characters: int = 12000) -> dict[str, str]: ...


class FileEventSource:
    """Bounded metadata-only polling for explicitly authorized file triggers."""

    def __init__(
        self,
        store: ScheduleStore,
        file_policy: FileAccessPolicyStore,
        *,
        max_files_per_trigger: int = 1_000,
    ) -> None:
        self.store = store
        self.file_policy = file_policy
        self.max_files_per_trigger = max(1, min(max_files_per_trigger, 10_000))
        self._snapshots: dict[tuple[str, int], dict[str, tuple[int, int]]] = {}

    async def poll(self) -> int:
        materialized = 0
        active_keys = set()
        for schedule in self.store.list(statuses=(ScheduleStatus.ACTIVE,)):
            trigger = schedule.trigger
            if not isinstance(trigger, EventTrigger) or trigger.observation_type != "file":
                continue
            key = (str(schedule.schedule_id), schedule.revision)
            active_keys.add(key)
            if self.file_policy.get().denial_reason("filesystem.read", trigger.target):
                self._snapshots.pop(key, None)
                continue
            current = _snapshot(resolve_user_path(trigger.target), self.max_files_per_trigger)
            previous = self._snapshots.get(key)
            self._snapshots[key] = current
            if previous is None:
                continue
            events = []
            if "created" in trigger.events:
                events.extend(
                    (path, "created", current[path]) for path in current.keys() - previous
                )
            if "modified" in trigger.events:
                events.extend(
                    (path, "modified", current[path])
                    for path in current.keys() & previous
                    if current[path] != previous[path]
                )
            if "deleted" in trigger.events:
                events.extend(
                    (path, "deleted", previous[path]) for path in previous.keys() - current
                )
            for path, event, metadata in sorted(events):
                occurred_at = aware_utc(self.store.clock.now())
                identity = hashlib.sha256(
                    f"{schedule.schedule_id}|{schedule.revision}|{path}|{event}|{metadata}".encode()
                ).hexdigest()[:48]
                runs = self.store.observe(
                    FileObservation(
                        observation_id=f"file:{identity}",
                        occurred_at=occurred_at,
                        target=path,
                        event=event,
                        size_bytes=None if event == "deleted" else metadata[1],
                    )
                )
                materialized += len(runs)
        for key in set(self._snapshots) - active_keys:
            self._snapshots.pop(key, None)
        return materialized


class ProcessEventSource:
    """Observe exact process-name starts/stops without persisting process lists."""

    def __init__(self, store: ScheduleStore, adapter: ProcessAdapter) -> None:
        self.store = store
        self.adapter = adapter
        self._snapshots: dict[tuple[str, int], set[int]] = {}

    async def poll(self) -> int:
        materialized = 0
        active_keys = set()
        for schedule in self.store.list(statuses=(ScheduleStatus.ACTIVE,)):
            trigger = schedule.trigger
            if not isinstance(trigger, EventTrigger) or trigger.observation_type != "process":
                continue
            key = (str(schedule.schedule_id), schedule.revision)
            active_keys.add(key)
            try:
                rows = await self.adapter.list(trigger.target)
            except (OSError, RuntimeError):
                continue
            current = {
                row.pid for row in rows if row.image_name.casefold() == trigger.target.casefold()
            }
            previous = self._snapshots.get(key)
            self._snapshots[key] = current
            if previous is None:
                continue
            events = []
            if "started" in trigger.events:
                events.extend((pid, "started") for pid in current - previous)
            if "stopped" in trigger.events:
                events.extend((pid, "stopped") for pid in previous - current)
            for pid, event in sorted(events):
                identity = _event_identity(schedule.schedule_id, schedule.revision, pid, event)
                materialized += len(
                    self.store.observe(
                        ProcessObservation(
                            observation_id=f"process:{identity}",
                            occurred_at=aware_utc(self.store.clock.now()),
                            target=trigger.target,
                            event=event,
                            process_id=pid,
                        )
                    )
                )
        _remove_inactive(self._snapshots, active_keys)
        return materialized


class DeviceEventSource:
    """Observe one exact connected-device identity and bounded status fingerprint."""

    def __init__(self, store: ScheduleStore, adapter: WindowsDeviceAdapter) -> None:
        self.store = store
        self.adapter = adapter
        self._snapshots: dict[tuple[str, int], tuple[str, str] | None] = {}

    async def poll(self) -> int:
        materialized = 0
        active_keys = set()
        for schedule in self.store.list(statuses=(ScheduleStatus.ACTIVE,)):
            trigger = schedule.trigger
            if not isinstance(trigger, EventTrigger) or trigger.observation_type != "device":
                continue
            key = (str(schedule.schedule_id), schedule.revision)
            active_keys.add(key)
            try:
                devices = await self.adapter.connected_devices(limit=200)
            except (OSError, RuntimeError):
                continue
            selected = next(
                (
                    item
                    for item in devices
                    if str(item.get("instance_id") or "").casefold() == trigger.target.casefold()
                ),
                None,
            )
            current = _device_state(selected)
            had_previous = key in self._snapshots
            previous = self._snapshots.get(key)
            self._snapshots[key] = current
            if not had_previous:
                continue
            event = None
            if previous is None and current is not None and "connected" in trigger.events:
                event = "connected"
            elif previous is not None and current is None and "disconnected" in trigger.events:
                event = "disconnected"
            elif (
                previous is not None
                and current is not None
                and previous != current
                and "changed" in trigger.events
            ):
                event = "changed"
            if event is None:
                continue
            identity = _event_identity(
                schedule.schedule_id, schedule.revision, trigger.target, event, current
            )
            materialized += len(
                self.store.observe(
                    DeviceObservation(
                        observation_id=f"device:{identity}",
                        occurred_at=aware_utc(self.store.clock.now()),
                        target=trigger.target,
                        event=event,
                        value=current[0] if current is not None else "disconnected",
                    )
                )
            )
        _remove_inactive(self._snapshots, active_keys)
        return materialized


class WebEventSource:
    """Observe a bounded normalized text fingerprint for exact public HTTPS targets."""

    def __init__(
        self,
        store: ScheduleStore,
        client: WebFetcher | None = None,
        *,
        max_characters: int = 12_000,
    ) -> None:
        self.store = store
        self.client = client or WebClient()
        self.max_characters = max(1_000, min(max_characters, 50_000))
        self._snapshots: dict[tuple[str, int], str] = {}

    async def poll(self) -> int:
        materialized = 0
        active_keys = set()
        for schedule in self.store.list(statuses=(ScheduleStatus.ACTIVE,)):
            trigger = schedule.trigger
            if not isinstance(trigger, EventTrigger) or trigger.observation_type != "web":
                continue
            key = (str(schedule.schedule_id), schedule.revision)
            active_keys.add(key)
            try:
                payload = await self.client.fetch(
                    trigger.target, max_characters=self.max_characters
                )
            except (OSError, RuntimeError, ValueError):
                continue
            current = hashlib.sha256(payload.get("text", "").encode("utf-8")).hexdigest()
            previous = self._snapshots.get(key)
            self._snapshots[key] = current
            if previous is None or previous == current or "changed" not in trigger.events:
                continue
            identity = _event_identity(schedule.schedule_id, schedule.revision, current)
            materialized += len(
                self.store.observe(
                    WebObservation(
                        observation_id=f"web:{identity}",
                        occurred_at=aware_utc(self.store.clock.now()),
                        target=trigger.target,
                        content_fingerprint=current,
                    )
                )
            )
        _remove_inactive(self._snapshots, active_keys)
        return materialized


def _snapshot(root: Path, limit: int) -> dict[str, tuple[int, int]]:
    try:
        if root.is_file():
            stat = root.stat()
            return {str(root.resolve()): (stat.st_mtime_ns, stat.st_size)}
        if not root.is_dir():
            return {}
        values = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            values[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
            if len(values) >= limit:
                break
        return values
    except OSError:
        return {}


def _event_identity(*values: object) -> str:
    payload = "|".join(str(value) for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:48]


def _remove_inactive(snapshots: dict, active_keys: set[tuple[str, int]]) -> None:
    for key in set(snapshots) - active_keys:
        snapshots.pop(key, None)


def _device_state(device: dict | None) -> tuple[str, str] | None:
    if device is None:
        return None
    status = str(device.get("status") or "unknown")[:100]
    bounded = {
        key: str(device.get(key) or "")[:500] for key in ("instance_id", "name", "class", "status")
    }
    fingerprint = hashlib.sha256(
        json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return status, fingerprint
