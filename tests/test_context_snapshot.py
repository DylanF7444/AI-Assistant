import asyncio

import pytest

from assistant.context.budget import ContextBudget
from assistant.context.providers import (
    DesktopContextProvider,
    DeviceContextProvider,
    FileContextProvider,
    PermissionContextProvider,
    PreferenceContextProvider,
    ProcessContextProvider,
    SessionContextProvider,
    SkillContextProvider,
    collect_context_snapshot,
)
from assistant.context.snapshot import ContextItem, ContextSnapshot
from assistant.contracts import RequestResultItem
from assistant.policy.file_access import FileAccessPolicyStore
from assistant.policy.permissions import GrantType, PermissionManager
from assistant.skills.builtin import CalculatorSkill, FileSearchSkill
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import AuditStore, Database, SettingsStore
from assistant.storage.memory import MemoryStore
from assistant.windows.adapters import ProcessInfo


class StaticProvider:
    name = "static"

    async def collect(self, request_text, *, session_id, budget):
        return [
            ContextItem(
                key="session.recent_interactions",
                title="Recent interactions",
                value=[{"request": "hello", "response": "Hi."}],
                sensitivity="local",
                source=self.name,
                summary="One recent interaction.",
            ),
            ContextItem(
                key="clipboard.contents",
                title="Clipboard contents",
                value="secret note",
                sensitivity="sensitive",
                source=self.name,
                summary="Sensitive clipboard text.",
            ),
        ]


class FailingProvider:
    name = "broken"

    async def collect(self, request_text, *, session_id, budget):
        raise RuntimeError("boom")


class FakeDesktopAdapter:
    async def active_process_name(self):
        return "brave.exe"

    async def visible_windows(self):
        return [
            {"process_name": "brave.exe", "title": "Private Project Plan", "private": False},
            {"process_name": "msedge.exe", "title": "InPrivate", "private": True},
        ]

    @staticmethod
    def _monitor_work_areas():
        return [(0, 0, 1920, 1080), (1920, 0, 3840, 1080)]


class FakeProcessAdapter:
    async def list(self, name_filter=None):
        return [
            ProcessInfo("svchost.exe", 1, "Services", "1,000 K"),
            ProcessInfo("brave.exe", 2, "Console", "200,000 K"),
            ProcessInfo("python.exe", 3, "Console", "150,000 K"),
        ]


class FakeDeviceAdapter:
    async def battery_status(self):
        return {"percent": 87, "charging": True}

    async def performance_status(self):
        return {"cpu_percent": 5, "memory": {"percent_used": 40}}

    async def connected_devices(self, limit=50):
        return [{"FriendlyName": "USB Headset", "Class": "Audio", "Status": "OK"}]

    async def storage_status(self, drive=None):
        return [{"drive": "C:", "percent_free": 50}]


class CountingProcessAdapter(FakeProcessAdapter):
    def __init__(self):
        self.calls = 0

    async def list(self, name_filter=None):
        self.calls += 1
        return await super().list(name_filter)


class ConcurrentProvider:
    def __init__(self, name, started, ready):
        self.name = name
        self.started = started
        self.ready = ready

    async def collect(self, request_text, *, session_id, budget):
        self.started.append(self.name)
        if len(self.started) == 2:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=0.5)
        return [
            ContextItem(
                key=f"test.{self.name}",
                title=self.name,
                value=self.name,
                source=self.name,
            )
        ]


def test_context_snapshot_redacted_debug_items_hide_sensitive_values():
    snapshot = ContextSnapshot(
        request_text="Use this.",
        session_id="s1",
        items=[
            ContextItem(
                key="clipboard.contents",
                title="Clipboard contents",
                value="secret note",
                sensitivity="sensitive",
                source="test",
                summary="Clipboard text.",
            )
        ],
    )

    debug = snapshot.redacted_debug_items()

    assert debug == [
        {
            "key": "clipboard.contents",
            "title": "Clipboard contents",
            "sensitivity": "sensitive",
            "source": "test",
            "summary": "Clipboard text.",
            "value": "[redacted]",
        }
    ]


@pytest.mark.asyncio
async def test_collect_context_snapshot_omits_sensitive_items_by_default():
    snapshot = await collect_context_snapshot(
        "Clean up my downloads.",
        session_id="s1",
        providers=(StaticProvider(),),
    )

    assert [item.key for item in snapshot.items] == ["session.recent_interactions"]


@pytest.mark.asyncio
async def test_collect_context_snapshot_records_provider_failure_without_values():
    snapshot = await collect_context_snapshot(
        "Clean up my downloads.",
        session_id="s1",
        providers=(FailingProvider(),),
        budget=ContextBudget(include_provider_errors=True),
    )

    assert snapshot.items[0].key == "context.provider_error.broken"
    assert snapshot.items[0].sensitivity == "public"
    assert snapshot.items[0].value == {"provider": "broken", "error": "RuntimeError"}


@pytest.mark.asyncio
async def test_session_context_provider_collects_recent_and_file_state():
    provider = SessionContextProvider(
        recent_context=lambda: [{"request": "Find report", "intent": "windows.file_search"}],
        pending_clarification=lambda: {
            "original_text": "Find report",
            "clarification": "Which folder?",
        },
        selected_file=lambda: "C:/Users/Dylan/Downloads/report.pdf",
        file_candidates=lambda: ("C:/Users/Dylan/Downloads/report.pdf",),
    )

    items = await provider.collect(
        "Clean up my downloads.",
        session_id="s1",
        budget=ContextBudget(),
    )

    assert [item.key for item in items] == [
        "session.recent_interactions",
        "session.pending_clarification",
        "results.selected",
        "results.recent_items",
        "files.selected",
        "files.recent_candidates",
    ]
    assert items[0].source == "session"
    assert items[2].value["item_id"] == "file-1"
    assert items[3].value["items"][0]["reference"] == "item 1"


@pytest.mark.asyncio
async def test_session_context_provider_exposes_generic_selected_result_without_parsing_text():
    selected = RequestResultItem(
        item_id="task-42",
        title="Review the release",
        value={"id": 42, "completed": False},
        kind="task",
        metadata={"item_id": 42},
    )
    provider = SessionContextProvider(
        selected_result_item=lambda: selected,
        result_items=lambda: (selected,),
        result_source=lambda: "core.tasks",
    )

    items = await provider.collect(
        "Complete it.",
        session_id="s1",
        budget=ContextBudget(),
    )

    assert [item.key for item in items] == [
        "results.selected",
        "results.recent_items",
    ]
    assert items[0].value["source"] == "core.tasks"
    assert items[0].value["value"]["id"] == 42


@pytest.mark.asyncio
async def test_recent_result_context_stays_structured_when_item_values_are_long():
    results = tuple(
        RequestResultItem(
            item_id=f"news-{index}",
            title=f"Technology headline {index} with enough detail to identify it",
            value="https://news.example/" + ("long-path-" * 50) + str(index),
            kind="news_article",
            description="Example News — today",
            metadata={"url": "https://news.example/" + ("long-path-" * 50) + str(index)},
        )
        for index in range(1, 6)
    )
    provider = SessionContextProvider(
        result_items=lambda: results,
        result_source=lambda: "web.news_search",
    )

    snapshot = await collect_context_snapshot(
        "Tell me more about the first one",
        session_id="s1",
        providers=(provider,),
    )

    recent = snapshot.items_by_key()["results.recent_items"].value
    assert isinstance(recent, dict)
    assert [item["position"] for item in recent["items"]] == [1, 2, 3, 4, 5]
    assert recent["items"][0]["title"].startswith("Technology headline 1")


@pytest.mark.asyncio
async def test_skill_context_provider_excludes_disabled_skills():
    registry = SkillRegistry(require_signatures=False)
    registry.register(CalculatorSkill())
    registry.register(FileSearchSkill(object()))
    registry.disable("core.calculator")
    provider = SkillContextProvider(registry)

    items = await provider.collect(
        "Clean up my downloads.",
        session_id="s1",
        budget=ContextBudget(),
    )

    assert [item.key for item in items] == [
        "skills.available",
        "skills.risk_summary",
        "skills.permission_summary",
    ]
    available = items[0].value
    assert available == [
        {
            "id": "windows.file_search",
            "name": "File Search",
            "description": "Search filenames within an approved folder.",
            "risk": "low",
            "enabled": True,
        }
    ]
    assert items[1].value == {"low": ["windows.file_search"]}
    assert items[2].value["filesystem.read"] == ["windows.file_search"]


@pytest.mark.asyncio
async def test_permission_context_provider_summarizes_grants_and_file_policy(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    audit = AuditStore(database)
    file_policy = FileAccessPolicyStore(SettingsStore(database))
    file_policy.update_entry(
        list_name="whitelist",
        access="read",
        path=str(tmp_path),
        add=True,
    )
    manager = PermissionManager(database, audit, file_policy)
    manager.grant(
        user_id="local-user",
        skill_id="windows.file_search",
        capability="filesystem.read",
        grant_type=GrantType.ALWAYS,
        targets=(str(tmp_path),),
    )
    provider = PermissionContextProvider(
        manager,
        file_policy_store=file_policy,
        confirmation_overrides={"filesystem.write": True},
    )

    items = await provider.collect(
        "Clean up my downloads.",
        session_id="s1",
        budget=ContextBudget(),
    )

    assert [item.key for item in items] == [
        "permissions.granted",
        "permissions.file_policy",
        "permissions.confirmation_required",
    ]
    assert items[0].value[0]["skill_id"] == "windows.file_search"
    assert items[1].value["read_whitelist"]
    assert items[2].value == {"filesystem.write": True}


@pytest.mark.asyncio
async def test_preference_context_provider_collects_enabled_memory_only(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    store = MemoryStore(database)
    remembered = store.remember(
        type="preference",
        key="browser",
        value="User prefers Brave for browsing.",
        source="user",
        confidence=1,
    )
    store.remember(
        type="constraint",
        key="files",
        value="Ask before organizing files.",
        source="user",
        confidence=1,
    )
    store.forget(memory_id=remembered.id)
    provider = PreferenceContextProvider(store)

    items = await provider.collect(
        "Use my usual browser.",
        session_id="s1",
        budget=ContextBudget(),
    )

    assert [item.key for item in items] == ["memory.constraints"]
    assert items[0].value == [
        {
            "id": 2,
            "type": "constraint",
            "key": "files",
            "value": "Ask before organizing files.",
            "confidence": 1,
        }
    ]


@pytest.mark.asyncio
async def test_desktop_context_provider_obeys_flag_and_redacts_titles():
    disabled = DesktopContextProvider(
        FakeDesktopAdapter(),
        include_open_applications=False,
    )
    assert await disabled.collect("move brave", session_id="s1", budget=ContextBudget()) == []

    provider = DesktopContextProvider(FakeDesktopAdapter())
    items = await provider.collect("move brave", session_id="s1", budget=ContextBudget())

    assert [item.key for item in items] == [
        "desktop.focused_application",
        "desktop.visible_windows",
        "desktop.monitor_layout",
    ]
    assert items[0].value == {"process_name": "brave.exe"}
    assert items[1].value[0]["title"] == "[omitted]"
    assert items[2].value[1]["monitor"] == 2


@pytest.mark.asyncio
async def test_process_context_provider_filters_noisy_background_processes():
    provider = ProcessContextProvider(FakeProcessAdapter())

    items = await provider.collect("fix this app", session_id="s1", budget=ContextBudget())

    assert [item.key for item in items] == [
        "processes.user_apps",
        "processes.high_resource",
    ]
    assert [process["image_name"] for process in items[0].value] == [
        "brave.exe",
        "python.exe",
    ]
    assert items[1].value[0]["image_name"] == "brave.exe"


@pytest.mark.asyncio
async def test_device_context_provider_obeys_flag_and_summarizes_devices():
    disabled = DeviceContextProvider(
        FakeDeviceAdapter(),
        include_connected_devices=False,
    )
    assert await disabled.collect("turn that down", session_id="s1", budget=ContextBudget()) == []

    provider = DeviceContextProvider(FakeDeviceAdapter())
    items = await provider.collect("turn that down", session_id="s1", budget=ContextBudget())

    assert [item.key for item in items] == [
        "devices.battery",
        "devices.performance",
        "devices.connected",
        "devices.storage",
    ]
    assert items[2].value == [{"FriendlyName": "USB Headset", "Class": "Audio", "Status": "OK"}]


@pytest.mark.asyncio
async def test_file_context_provider_summarizes_folders_without_reading_contents(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "report.pdf").write_text("secret report body")
    (downloads / "notes.txt").write_text("secret notes body")
    provider = FileContextProvider(
        object(),
        known_folders={"downloads": str(downloads)},
    )

    items = await provider.collect(
        "clean up downloads",
        session_id="s1",
        budget=ContextBudget(),
    )

    assert [item.key for item in items] == [
        "files.known_folders",
        "files.folder_summaries",
    ]
    summary = items[1].value[0]
    assert summary["extensions"] == {".pdf": 1, ".txt": 1}
    assert summary["contents_read"] is False
    assert "secret" not in str(items)


@pytest.mark.asyncio
async def test_context_providers_collect_concurrently_but_preserve_provider_order():
    started = []
    ready = asyncio.Event()

    snapshot = await collect_context_snapshot(
        "Use relevant context.",
        session_id="s1",
        providers=(
            ConcurrentProvider("first", started, ready),
            ConcurrentProvider("second", started, ready),
        ),
    )

    assert set(started) == {"first", "second"}
    assert [item.key for item in snapshot.items] == ["test.first", "test.second"]


@pytest.mark.asyncio
async def test_candidate_skills_skip_irrelevant_expensive_context_and_cache_relevant_results():
    adapter = CountingProcessAdapter()
    provider = ProcessContextProvider(adapter, cache_ttl_seconds=30)

    irrelevant = await collect_context_snapshot(
        "What is the capital of France?",
        session_id="s1",
        providers=(provider,),
        candidate_skill_ids=("web.search",),
    )
    first = await collect_context_snapshot(
        "Which applications are running?",
        session_id="s1",
        providers=(provider,),
        candidate_skill_ids=("windows.process",),
    )
    second = await collect_context_snapshot(
        "Which applications are running?",
        session_id="s1",
        providers=(provider,),
        candidate_skill_ids=("windows.process",),
    )

    assert irrelevant.items == []
    assert [item.key for item in first.items] == [
        "processes.user_apps",
        "processes.high_resource",
    ]
    assert [item.key for item in second.items] == [
        "processes.user_apps",
        "processes.high_resource",
    ]
    assert adapter.calls == 1
