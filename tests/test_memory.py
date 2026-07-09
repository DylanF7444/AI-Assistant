import asyncio

import pytest

from assistant.contracts import PermissionContext, ResultStatus, SkillCall
from assistant.language.embeddings import LexicalFallbackEmbeddingProvider
from assistant.language.retrieval import MemorySemanticIndexer, SemanticRetriever
from assistant.skills.memory import MemorySkill
from assistant.storage.database import Database
from assistant.storage.memory import MemoryStore
from assistant.storage.vector_index import VectorIndex


def test_memory_store_remember_search_and_forget(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    store = MemoryStore(database)

    item = store.remember(
        type="preference",
        key="browser",
        value="User prefers Brave for browsing.",
        source="user",
        confidence=1,
    )

    assert item.id is not None
    assert store.search("brave")[0].key == "browser"
    assert store.list(type="preference")[0].value == "User prefers Brave for browsing."

    assert store.forget(memory_id=item.id)
    assert store.search("brave") == ()
    assert store.list(include_disabled=True)[0].enabled is False


def test_memory_store_can_update_and_delete(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    store = MemoryStore(database)
    item = store.remember(
        type="preference",
        key="browser",
        value="User prefers Brave for browsing.",
        source="user",
        confidence=1,
    )

    updated = store.update(
        memory_id=item.id,
        value="User prefers Firefox for browsing.",
        confidence=0.8,
    )

    assert updated.value == "User prefers Firefox for browsing."
    assert updated.confidence == 0.8
    assert store.delete(memory_id=item.id)
    assert store.list(include_disabled=True) == ()


@pytest.mark.asyncio
async def test_memory_skill_requires_explicit_remember_and_can_list_and_forget(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    skill = MemorySkill(MemoryStore(database))

    remembered = await skill.execute(
        _call(
            {
                "operation": "remember",
                "type": "preference",
                "key": "browser",
                "value": "User prefers Brave for browsing.",
            }
        ),
        asyncio.Event(),
    )

    assert remembered.status == ResultStatus.COMPLETED
    assert remembered.data["memories"][0]["key"] == "browser"
    assert "I will remember" in remembered.summary

    listed = await skill.execute(
        _call({"operation": "list", "query": "brave"}),
        asyncio.Event(),
    )
    memory_id = listed.data["memories"][0]["id"]
    assert listed.data["memories"][0]["value"] == "User prefers Brave for browsing."

    forgotten = await skill.execute(
        _call({"operation": "forget", "memory_id": memory_id}),
        asyncio.Event(),
    )

    assert forgotten.status == ResultStatus.COMPLETED
    assert forgotten.data["memories"] == []
    assert "Forgot" in forgotten.summary


@pytest.mark.asyncio
async def test_memory_skill_can_forget_by_query(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    skill = MemorySkill(MemoryStore(database))
    await skill.execute(
        _call(
            {
                "operation": "remember",
                "type": "preference",
                "key": "browser",
                "value": "User prefers Brave for browsing.",
            }
        ),
        asyncio.Event(),
    )

    forgotten = await skill.execute(
        _call({"operation": "forget", "query": "Brave browser"}),
        asyncio.Event(),
    )
    listed = await skill.execute(_call({"operation": "list"}), asyncio.Event())

    assert "Forgot" in forgotten.summary
    assert listed.data["memories"] == []


@pytest.mark.asyncio
async def test_memory_skill_can_inspect_edit_and_delete(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    skill = MemorySkill(MemoryStore(database))
    remembered = await skill.execute(
        _call(
            {
                "operation": "remember",
                "type": "preference",
                "key": "browser",
                "value": "User prefers Brave for browsing.",
            }
        ),
        asyncio.Event(),
    )
    memory_id = remembered.data["memories"][0]["id"]

    inspected = await skill.execute(
        _call({"operation": "inspect", "memory_id": memory_id}),
        asyncio.Event(),
    )
    edited = await skill.execute(
        _call(
            {
                "operation": "edit",
                "memory_id": memory_id,
                "value": "User prefers Firefox for browsing.",
                "confidence": 0.75,
            }
        ),
        asyncio.Event(),
    )
    deleted = await skill.execute(
        _call({"operation": "delete", "memory_id": memory_id}),
        asyncio.Event(),
    )

    assert inspected.data["memories"][0]["value"] == "User prefers Brave for browsing."
    assert edited.data["memories"][0]["value"] == "User prefers Firefox for browsing."
    assert edited.data["memories"][0]["confidence"] == 0.75
    assert "Deleted" in deleted.summary
    assert deleted.data["memories"] == []


@pytest.mark.asyncio
async def test_memory_skill_requires_confirmation_for_sensitive_values(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    skill = MemorySkill(MemoryStore(database))

    denied = await skill.execute(
        _call(
            {
                "operation": "remember",
                "type": "preference",
                "key": "api_key",
                "value": "My API key is secret-token",
            }
        ),
        asyncio.Event(),
    )
    confirmed = await skill.execute(
        _call(
            {
                "operation": "remember",
                "type": "preference",
                "key": "api_key",
                "value": "My API key is secret-token",
                "confirm_sensitive": True,
            }
        ),
        asyncio.Event(),
    )

    assert denied.status == ResultStatus.DENIED
    assert "sensitive" in denied.summary
    assert confirmed.status == ResultStatus.COMPLETED
    assert confirmed.data["memories"][0]["key"] == "api_key"


@pytest.mark.asyncio
async def test_memory_index_updates_after_edit_and_delete(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    store = MemoryStore(database)
    item = store.remember(
        type="preference",
        key="browser",
        value="User prefers Brave for browsing.",
        source="user",
        confidence=1,
    )
    index = VectorIndex(database)
    provider = LexicalFallbackEmbeddingProvider()
    indexer = MemorySemanticIndexer(index, provider)
    retriever = SemanticRetriever(index, provider)

    await indexer.refresh(store)
    assert await retriever.search("brave browser", namespaces={"memory"}, limit=5)

    store.update(memory_id=item.id, value="User prefers Firefox for browsing.")
    await indexer.refresh(store)
    brave = await retriever.search("brave browser", namespaces={"memory"}, limit=5)
    firefox = await retriever.search("firefox browser", namespaces={"memory"}, limit=5)

    assert not any("Brave" in item.text for item in brave)
    assert any("Firefox" in item.text for item in firefox)

    store.delete(memory_id=item.id)
    await indexer.refresh(store)

    assert await retriever.search("firefox browser", namespaces={"memory"}, limit=5) == ()


def _call(arguments):
    return SkillCall(
        skill_id="core.memory",
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )
