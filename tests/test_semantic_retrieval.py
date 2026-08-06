import pytest

from assistant.language.embeddings import (
    EmbeddingProvider as LanguageEmbeddingProvider,
)
from assistant.language.embeddings import (
    FallbackEmbeddingProvider,
    LexicalFallbackEmbeddingProvider,
)
from assistant.language.retrieval import (
    KnowledgeSemanticIndexer,
    MemorySemanticIndexer,
    SemanticRetriever,
    SkillSemanticIndexer,
    WorkflowTemplateSemanticIndexer,
)
from assistant.providers import EmbeddingProvider
from assistant.skills.builtin import CalculatorSkill
from assistant.skills.examples import SkillExample
from assistant.skills.network import HeadlessReadSkill, WebFetchSkill, WebSearchSkill
from assistant.skills.registry import SkillRegistry
from assistant.skills.visualize import VisualizeSkill
from assistant.skills.windows import ApplicationWindowSkill
from assistant.storage.database import Database, VectorItemRow
from assistant.storage.knowledge import KnowledgeBaseStore
from assistant.storage.memory import MemoryStore
from assistant.storage.vector_index import VectorIndex


def test_language_embedding_protocol_reexports_canonical_provider_contract():
    assert LanguageEmbeddingProvider is EmbeddingProvider


class FakeEmbeddingProvider:
    def __init__(self, vectors):
        self.vectors = vectors
        self.calls = []

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        return [self.vectors[text] for text in texts]


class KeywordEmbeddingProvider:
    async def embed(self, texts):
        vectors = []
        for text in texts:
            folded = text.casefold()
            if "get this out of the way" in folded:
                vectors.append([1.0, 0.0])
            elif "minimize_focused" in folded:
                vectors.append([0.8, 0.2])
            elif "calculator" in folded:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.5, 0.5])
        return vectors


class FailingEmbeddingProvider:
    async def embed(self, texts):
        raise RuntimeError("embedding backend unavailable")


@pytest.mark.asyncio
async def test_fallback_embedding_provider_uses_lexical_when_primary_fails():
    provider = FallbackEmbeddingProvider(
        FailingEmbeddingProvider(),
        LexicalFallbackEmbeddingProvider(dimensions=16),
    )

    vectors = await provider.embed(["launch browser"])

    assert len(vectors) == 1
    assert len(vectors[0]) == 16


def test_vector_index_upserts_and_searches_by_cosine(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    index = VectorIndex(database)
    index.upsert(
        namespace="skill",
        source_id="windows.launcher",
        text="Open an application",
        metadata={"skill_id": "windows.launcher"},
        embedding=[1.0, 0.0],
        embedding_model="test",
    )
    index.upsert(
        namespace="skill",
        source_id="core.calculator",
        text="Calculate arithmetic",
        metadata={"skill_id": "core.calculator"},
        embedding=[0.0, 1.0],
        embedding_model="test",
    )

    results = index.search([0.9, 0.1], namespaces={"skill"}, limit=2)

    assert [item.source_id for item in results] == [
        "windows.launcher",
        "core.calculator",
    ]
    assert results[0].metadata == {"skill_id": "windows.launcher"}
    assert results[0].score > results[1].score

    index.upsert(
        namespace="skill",
        source_id="core.calculator",
        text="Updated calculator text",
        metadata={"updated": True},
        embedding=[1.0, 0.0],
        embedding_model="test",
    )

    assert index.search([1.0, 0.0], namespaces={"skill"}, limit=1)[0].metadata == {"updated": True}


@pytest.mark.asyncio
async def test_semantic_retriever_embeds_query_and_filters_namespaces(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    index = VectorIndex(database)
    index.upsert(
        namespace="skill",
        source_id="windows.application_window",
        text="Minimize the focused window",
        metadata={"operation": "minimize_focused"},
        embedding=[1.0, 0.0],
        embedding_model="test",
    )
    index.upsert(
        namespace="knowledge",
        source_id="note-1",
        text="Window gardening notes",
        metadata={},
        embedding=[1.0, 0.0],
        embedding_model="test",
    )
    provider = FakeEmbeddingProvider({"get this out of the way": [1.0, 0.0]})
    retriever = SemanticRetriever(index, provider)

    results = await retriever.search(
        "get this out of the way",
        namespaces={"skill"},
        limit=3,
    )

    assert len(results) == 1
    assert results[0].namespace == "skill"
    assert results[0].source_id == "windows.application_window"
    assert results[0].metadata == {"operation": "minimize_focused"}
    assert provider.calls == [("get this out of the way",)]


@pytest.mark.asyncio
async def test_lexical_fallback_embedding_provider_is_deterministic_and_synonym_aware():
    provider = LexicalFallbackEmbeddingProvider(dimensions=64)
    query, browser, calculator = await provider.embed(
        [
            "launch my browser",
            "open the web browser",
            "calculate two plus two",
        ]
    )

    assert query == (await provider.embed(["launch my browser"]))[0]
    assert _cosine(query, browser) > _cosine(query, calculator)


@pytest.mark.asyncio
async def test_skill_semantic_indexer_indexes_enabled_skills_and_examples(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    registry = SkillRegistry(require_signatures=False)
    registry.register(CalculatorSkill())
    registry.register(ApplicationWindowSkill(object()))
    registry.disable("core.calculator")
    index = VectorIndex(database)
    indexer = SkillSemanticIndexer(
        index,
        KeywordEmbeddingProvider(),
        examples=(
            SkillExample(
                utterance="get this out of the way",
                skill_id="windows.application_window",
                arguments={"operation": "minimize_focused"},
                notes="Requires focused window context.",
            ),
            SkillExample(
                utterance="calculate numbers",
                skill_id="core.calculator",
            ),
        ),
    )

    await indexer.refresh(registry)
    retriever = SemanticRetriever(index, KeywordEmbeddingProvider())
    results = await retriever.search(
        "get this out of the way",
        namespaces={"skill", "skill_example"},
        limit=5,
    )

    assert results[0].namespace == "skill_example"
    assert results[0].metadata["skill_id"] == "windows.application_window"
    assert {(item.namespace, item.metadata["skill_id"]) for item in results} == {
        ("skill", "windows.application_window"),
        ("skill_example", "windows.application_window"),
    }


@pytest.mark.asyncio
async def test_skill_semantic_indexer_replaces_stale_skill_rows(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    registry = SkillRegistry(require_signatures=False)
    registry.register(CalculatorSkill())
    index = VectorIndex(database)
    indexer = SkillSemanticIndexer(index, KeywordEmbeddingProvider(), examples=())

    await indexer.refresh(registry)
    registry.disable("core.calculator")
    await indexer.refresh(registry)

    with database.session() as session:
        rows = list(session.query(VectorItemRow).all())
    assert rows == []


@pytest.mark.asyncio
async def test_memory_semantic_indexer_indexes_enabled_memories(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    store = MemoryStore(database)
    store.remember(
        type="preference",
        key="browser",
        value="User prefers Brave for browsing.",
        source="user",
        confidence=1,
    )
    disabled = store.remember(
        type="preference",
        key="editor",
        value="User prefers Notepad for notes.",
        source="user",
        confidence=1,
    )
    store.forget(memory_id=disabled.id)
    index = VectorIndex(database)
    indexer = MemorySemanticIndexer(index, KeywordEmbeddingProvider())

    await indexer.refresh(store)
    retriever = SemanticRetriever(index, KeywordEmbeddingProvider())
    results = await retriever.search(
        "use my usual browser",
        namespaces={"memory"},
        limit=5,
    )

    assert len(results) == 1
    assert results[0].namespace == "memory"
    assert results[0].metadata["type"] == "preference"
    assert results[0].metadata["key"] == "browser"
    assert "Brave" in results[0].text


@pytest.mark.asyncio
async def test_memory_semantic_indexer_removes_forgotten_memory_rows(tmp_path):
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
    indexer = MemorySemanticIndexer(index, KeywordEmbeddingProvider())

    await indexer.refresh(store)
    store.forget(memory_id=item.id)
    await indexer.refresh(store)

    assert index.search([0.5, 0.5], namespaces={"memory"}, limit=5) == ()


@pytest.mark.asyncio
async def test_knowledge_semantic_indexer_indexes_enabled_knowledge_only(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    store = KnowledgeBaseStore(database)
    enabled = store.save(
        "Runbooks",
        "Troubleshooting notes for the local assistant.",
        description="Assistant operations",
    )
    disabled = store.save(
        "Private Draft",
        "Hidden strategy notes.",
        enabled=False,
    )
    index = VectorIndex(database)
    indexer = KnowledgeSemanticIndexer(index, LexicalFallbackEmbeddingProvider())

    await indexer.refresh(store)
    results = await SemanticRetriever(
        index,
        LexicalFallbackEmbeddingProvider(),
    ).search("assistant troubleshooting", namespaces={"knowledge"}, limit=5)

    assert len(results) == 1
    assert results[0].metadata["knowledge_base_id"] == enabled.id

    store.set_enabled(enabled.id, False)
    store.set_enabled(disabled.id, True)
    await indexer.refresh(store)
    results = await SemanticRetriever(
        index,
        LexicalFallbackEmbeddingProvider(),
    ).search("hidden strategy", namespaces={"knowledge"}, limit=5)

    assert len(results) == 1
    assert results[0].metadata["knowledge_base_id"] == disabled.id


@pytest.mark.asyncio
async def test_workflow_template_indexer_adds_common_workflows(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    index = VectorIndex(database)
    provider = LexicalFallbackEmbeddingProvider()
    indexer = WorkflowTemplateSemanticIndexer(index, provider)

    await indexer.refresh()
    results = await SemanticRetriever(index, provider).search(
        "clean up my downloads",
        namespaces={"workflow_template"},
        limit=3,
    )

    assert results
    assert results[0].metadata["template_id"] == "organize_folder"


@pytest.mark.asyncio
async def test_research_visualization_workflow_exposes_its_enabled_skill_chain(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    registry = SkillRegistry(require_signatures=False)
    registry.register(WebSearchSkill(None, key=lambda _provider: None))
    registry.register(WebFetchSkill(None))
    registry.register(HeadlessReadSkill(object()))
    registry.register(VisualizeSkill())
    index = VectorIndex(database)
    provider = LexicalFallbackEmbeddingProvider()
    indexer = WorkflowTemplateSemanticIndexer(index, provider)

    await indexer.refresh(registry)
    results = await SemanticRetriever(index, provider).search(
        "show me a table of the highest earning movies this year",
        namespaces={"workflow_template"},
        limit=3,
    )

    research = next(
        item for item in results if item.metadata["template_id"] == "research_visualization"
    )
    assert research.metadata["skill_ids"] == [
        "web.search",
        "web.fetch",
        "web.headless_read",
        "core.visualize",
    ]
    assert research.metadata["skill_stages"] == [
        ["web.search"],
        ["web.fetch", "web.headless_read"],
        ["core.visualize"],
    ]
    assert research.metadata["completion_skill_ids"] == ["core.visualize"]


def _cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    return dot / (left_norm * right_norm)
