from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.contracts import PermissionContext, SkillCall
from assistant.policy.file_access import FileAccessPolicy, FileAccessPolicyStore
from assistant.scheduling.authoring import ScheduleAuthoringService
from assistant.scheduling.contracts import (
    AutonomyPolicy,
    EventTrigger,
    FileObservation,
    NotificationAction,
    ScheduleDefinition,
    ScheduleStatus,
    WebObservation,
)
from assistant.scheduling.event_sources import (
    DeviceEventSource,
    FileEventSource,
    ProcessEventSource,
    WebEventSource,
)
from assistant.scheduling.observations import matches
from assistant.scheduling.store import ScheduleStore
from assistant.skills.builtin import DateTimeSkill
from assistant.skills.registry import SkillRegistry
from assistant.skills.scheduling import ScheduleSkill
from assistant.storage.database import Database, SettingsStore
from assistant.storage.macros import MacroStore
from assistant.windows.interfaces import ProcessInfo


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, value: timedelta) -> None:
        self.value += value


def _call(arguments: dict) -> SkillCall:
    return SkillCall(
        skill_id="core.schedule",
        arguments=arguments,
        permission_context=PermissionContext(session_id="event-test"),
    )


@pytest.fixture
def event_store(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'events.db'}")
    database.migrate()
    clock = FakeClock(datetime(2026, 8, 7, 12, tzinfo=UTC))
    return ScheduleStore(database, clock), clock


@pytest.mark.asyncio
async def test_schedule_skill_authors_typed_file_trigger_with_separate_monitor_permission(
    event_store, tmp_path: Path
):
    store, _ = event_store
    registry = SkillRegistry()
    registry.register(DateTimeSkill())
    authoring = ScheduleAuthoringService(store, registry, MacroStore(store.database))
    skill = ScheduleSkill(store, authoring=authoring)
    root = tmp_path / "approved"
    root.mkdir()
    call = _call(
        {
            "operation": "create_action",
            "name": "Report file changes",
            "skill_id": "core.datetime",
            "arguments": {"query": "time"},
            "event": {
                "observation_type": "file",
                "target": str(root),
                "events": ["created", "modified"],
                "minimum_interval_seconds": 30,
            },
        }
    )

    permissions = skill.required_permissions(call)
    result = await skill.execute(call, asyncio.Event())
    schedule = store.get(result.data["schedules"][0]["schedule_id"])

    assert [scope.capability for scope in permissions] == [
        "filesystem.read",
        "automation.observe.file",
    ]
    assert isinstance(schedule.trigger, EventTrigger)
    assert schedule.next_run_at is None
    assert "automation.observe.file" in schedule.autonomy_policy.allowed_capabilities
    assert str(root) in schedule.autonomy_policy.allowed_targets


def test_typed_observation_materializes_once_and_honors_debounce(event_store, tmp_path: Path):
    store, clock = event_store
    root = tmp_path / "approved"
    root.mkdir()
    now = clock.now()
    definition = ScheduleDefinition(
        name="Watch files",
        status=ScheduleStatus.PENDING_AUTHORIZATION,
        trigger=EventTrigger(
            observation_type="file",
            target=str(root),
            events=("modified",),
            minimum_interval_seconds=30,
        ),
        action=NotificationAction(title="Changed", message="An approved file changed."),
        autonomy_policy=AutonomyPolicy(
            allowed_capabilities=("filesystem.read", "automation.observe.file"),
            allowed_targets=(str(root),),
        ),
        created_at=now,
        updated_at=now,
    )
    schedule = store.create(definition, authorize=True)
    first = FileObservation(
        observation_id="watch.1",
        occurred_at=now,
        target=str(root / "report.txt"),
        event="modified",
        size_bytes=42,
    )
    second = first.model_copy(
        update={"observation_id": "watch.2", "occurred_at": now + timedelta(seconds=10)}
    )

    created = store.observe(first)
    duplicate = store.observe(first)
    debounced = store.observe(second)
    clock.advance(timedelta(seconds=31))
    after_debounce = store.observe(
        second.model_copy(update={"occurred_at": clock.now(), "observation_id": "watch.3"})
    )

    assert len(created) == 1
    assert created[0].schedule_id == schedule.schedule_id
    assert ":event:watch.1" in created[0].occurrence_key
    assert duplicate == ()
    assert debounced == ()
    assert len(after_debounce) == 1


def test_observation_never_expands_target_or_turns_payload_into_prompt(event_store, tmp_path: Path):
    store, clock = event_store
    approved = tmp_path / "approved"
    approved.mkdir()
    other = tmp_path / "other" / "instructions.txt"
    observation = FileObservation(
        observation_id="outside.1",
        occurred_at=clock.now(),
        target=str(other),
        event="created",
    )
    trigger = EventTrigger(
        observation_type="file",
        target=str(approved),
        events=("created",),
    )

    assert matches(trigger, observation) is False
    assert not hasattr(observation, "prompt")


def test_web_observation_matches_only_sanitized_https_origin_and_path(event_store):
    _, clock = event_store
    trigger = EventTrigger(
        observation_type="web",
        target="https://status.example.com/incidents",
        events=("changed",),
    )
    matching = WebObservation(
        observation_id="web.1",
        occurred_at=clock.now(),
        target="https://status.example.com/incidents?token=redacted",
        content_fingerprint="a" * 64,
    )
    wrong_origin = matching.model_copy(
        update={"observation_id": "web.2", "target": "https://evil.example/incidents"}
    )

    assert matches(trigger, matching) is True
    assert matches(trigger, wrong_origin) is False


@pytest.mark.asyncio
async def test_file_event_source_polls_metadata_only_and_respects_policy(
    event_store, tmp_path: Path
):
    store, clock = event_store
    root = tmp_path / "approved"
    root.mkdir()
    policy = FileAccessPolicyStore(SettingsStore(store.database))
    now = clock.now()
    store.create(
        ScheduleDefinition(
            name="Watch approved folder",
            status=ScheduleStatus.PENDING_AUTHORIZATION,
            trigger=EventTrigger(
                observation_type="file",
                target=str(root),
                events=("created",),
                minimum_interval_seconds=1,
            ),
            action=NotificationAction(title="Changed", message="A file changed."),
            autonomy_policy=AutonomyPolicy(
                allowed_capabilities=("filesystem.read", "automation.observe.file"),
                allowed_targets=(str(root),),
            ),
            created_at=now,
            updated_at=now,
        ),
        authorize=True,
    )
    source = FileEventSource(store, policy)

    assert await source.poll() == 0
    (root / "new.txt").write_text("private contents are never read", encoding="utf-8")
    clock.advance(timedelta(seconds=2))
    assert await source.poll() == 1

    policy.set(FileAccessPolicy(read_blacklist=(str(root),)))
    (root / "blocked.txt").write_text("blocked", encoding="utf-8")
    clock.advance(timedelta(seconds=2))
    assert await source.poll() == 0


def _event_schedule(store, clock, trigger: EventTrigger):
    return store.create(
        ScheduleDefinition(
            name=f"Watch {trigger.observation_type}",
            status=ScheduleStatus.PENDING_AUTHORIZATION,
            trigger=trigger,
            action=NotificationAction(title="Changed", message="Observed a typed event."),
            autonomy_policy=AutonomyPolicy(
                allowed_capabilities=(f"automation.observe.{trigger.observation_type}",),
                allowed_targets=(trigger.target,),
            ),
            created_at=clock.now(),
            updated_at=clock.now(),
        ),
        authorize=True,
    )


@pytest.mark.asyncio
async def test_process_event_source_emits_exact_started_and_stopped_events(event_store):
    store, clock = event_store
    _event_schedule(
        store,
        clock,
        EventTrigger(
            observation_type="process",
            target="notepad.exe",
            events=("started", "stopped"),
            minimum_interval_seconds=1,
        ),
    )

    class Processes:
        rows = []

        async def list(self, _name_filter=None):
            return self.rows

    adapter = Processes()
    source = ProcessEventSource(store, adapter)
    assert await source.poll() == 0
    adapter.rows = [ProcessInfo("notepad.exe", 42, "Console", "1,000 K")]
    clock.advance(timedelta(seconds=2))
    assert await source.poll() == 1
    adapter.rows = []
    clock.advance(timedelta(seconds=2))
    assert await source.poll() == 1


@pytest.mark.asyncio
async def test_device_event_source_retains_only_bounded_status_fingerprint(event_store):
    store, clock = event_store
    _event_schedule(
        store,
        clock,
        EventTrigger(
            observation_type="device",
            target="USB\\VID_TEST",
            events=("connected", "changed", "disconnected"),
            minimum_interval_seconds=1,
        ),
    )

    class Devices:
        rows = []

        async def connected_devices(self, limit=50):
            assert limit == 200
            return self.rows

    adapter = Devices()
    source = DeviceEventSource(store, adapter)
    assert await source.poll() == 0
    adapter.rows = [
        {
            "instance_id": "USB\\VID_TEST",
            "name": "Test device",
            "class": "USB",
            "status": "OK",
            "private_payload": "must not be retained",
        }
    ]
    clock.advance(timedelta(seconds=2))
    assert await source.poll() == 1
    assert "private_payload" not in repr(source._snapshots)
    adapter.rows[0]["status"] = "Warning"
    clock.advance(timedelta(seconds=2))
    assert await source.poll() == 1


@pytest.mark.asyncio
async def test_web_event_source_sanitizes_target_and_emits_content_hash_only(event_store):
    store, clock = event_store
    schedule = _event_schedule(
        store,
        clock,
        EventTrigger(
            observation_type="web",
            target="https://EXAMPLE.com/status?secret=discarded#fragment",
            events=("changed",),
            minimum_interval_seconds=1,
        ),
    )
    assert schedule.trigger.target == "https://example.com/status"

    class Fetcher:
        text = "green"

        async def fetch(self, url, max_characters=12000):
            assert url == "https://example.com/status"
            assert max_characters == 12_000
            return {"url": url, "text": self.text}

    fetcher = Fetcher()
    source = WebEventSource(store, fetcher)
    assert await source.poll() == 0
    fetcher.text = "red"
    clock.advance(timedelta(seconds=2))
    assert await source.poll() == 1
    assert "red" not in repr(source._snapshots)
    assert "secret" not in schedule.model_dump_json()
