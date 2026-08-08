from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from assistant.language.embeddings import EmbeddingProvider
from assistant.skills.examples import DEFAULT_SKILL_EXAMPLES, SkillExample
from assistant.skills.registry import SkillRegistry
from assistant.skills.workflows import DEFAULT_WORKFLOW_TEMPLATES, WorkflowTemplate
from assistant.storage.knowledge import KnowledgeBaseStore
from assistant.storage.memory import MemoryItem, MemoryStore
from assistant.storage.playbooks import Playbook, PlaybookStore
from assistant.storage.vector_index import VectorIndex


def _embedding_signature(provider: EmbeddingProvider) -> str:
    values = [provider.__class__.__name__]
    for attribute in ("model", "model_name", "dimensions"):
        value = getattr(provider, attribute, None)
        if value is not None:
            values.append(f"{attribute}={value}")
    primary = getattr(provider, "primary", None)
    fallback = getattr(provider, "fallback", None)
    if primary is not None:
        values.append(f"primary={_embedding_signature(primary)}")
    if fallback is not None:
        values.append(f"fallback={_embedding_signature(fallback)}")
    return "|".join(values)


def _signature_digest(value: Any, provider: EmbeddingProvider) -> str:
    payload = json.dumps(
        {"content": value, "embedding": _embedding_signature(provider)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RetrievedContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str
    source_id: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float


class SemanticRetriever:
    DEFAULT_MINIMUM_SCORES = {
        "skill": 0.05,
        "skill_example": 0.08,
        "workflow_template": 0.08,
        "playbook": 0.10,
        "knowledge": 0.08,
        "memory": 0.08,
    }

    def __init__(
        self,
        index: VectorIndex,
        embedding_provider: EmbeddingProvider,
        *,
        minimum_scores: dict[str, float] | None = None,
    ) -> None:
        self.index = index
        self.embedding_provider = embedding_provider
        self.minimum_scores = {
            **self.DEFAULT_MINIMUM_SCORES,
            **(minimum_scores or {}),
        }

    async def search(
        self,
        query: str,
        *,
        namespaces: set[str],
        limit: int = 8,
    ) -> tuple[RetrievedContextItem, ...]:
        if limit <= 0 or not namespaces:
            return ()
        embeddings = await self.embedding_provider.embed([query])
        if not embeddings:
            return ()
        return tuple(
            RetrievedContextItem(
                namespace=item.namespace,
                source_id=item.source_id,
                text=item.text,
                metadata=item.metadata,
                score=item.score,
            )
            for item in self.index.search(
                embeddings[0],
                namespaces=namespaces,
                limit=max(limit * 3, limit),
            )
            if item.score >= self.minimum_scores.get(item.namespace, 0)
        )[:limit]


class SkillSemanticIndexer:
    def __init__(
        self,
        index: VectorIndex,
        embedding_provider: EmbeddingProvider,
        *,
        examples: tuple[SkillExample, ...] = DEFAULT_SKILL_EXAMPLES,
    ) -> None:
        self.index = index
        self.embedding_provider = embedding_provider
        self.examples = examples
        self._last_signature: tuple[Any, ...] | None = None

    async def refresh(self, registry: SkillRegistry) -> None:
        manifests = tuple(
            manifest for manifest in registry.list() if registry.is_enabled(manifest.skill_id)
        )
        enabled_skill_ids = {manifest.skill_id for manifest in manifests}
        examples = tuple(
            example for example in self.examples if example.skill_id in enabled_skill_ids
        )
        signature = (
            tuple(
                sorted(
                    (
                        manifest.skill_id,
                        manifest.version,
                        manifest.display_name,
                        manifest.description,
                        manifest.risk.value,
                        tuple(sorted(permission.capability for permission in manifest.permissions)),
                    )
                    for manifest in manifests
                )
            ),
            tuple(
                json.dumps(example.model_dump(mode="json"), sort_keys=True) for example in examples
            ),
        )
        signature_digest = _signature_digest(signature, self.embedding_provider)
        expected_count = len(manifests) + len(examples)
        if signature == self._last_signature or (
            self.index.stored_signature("skills") == signature_digest
            and self.index.namespace_count({"skill", "skill_example"}) == expected_count
        ):
            self._last_signature = signature
            return
        self.index.delete_namespace("skill")
        self.index.delete_namespace("skill_example")
        skill_texts = [_skill_text(manifest) for manifest in manifests]
        example_texts = [_example_text(example) for example in examples]
        embeddings = await self.embedding_provider.embed(skill_texts + example_texts)
        skill_embeddings = embeddings[: len(skill_texts)]
        example_embeddings = embeddings[len(skill_texts) :]
        model_name = self.embedding_provider.__class__.__name__
        for manifest, text, embedding in zip(
            manifests,
            skill_texts,
            skill_embeddings,
            strict=True,
        ):
            self.index.upsert(
                namespace="skill",
                source_id=manifest.skill_id,
                text=text,
                metadata={
                    "skill_id": manifest.skill_id,
                    "display_name": manifest.display_name,
                    "risk": manifest.risk.value,
                    "permissions": [permission.capability for permission in manifest.permissions],
                },
                embedding=embedding,
                embedding_model=model_name,
            )
        for index, (example, text, embedding) in enumerate(
            zip(examples, example_texts, example_embeddings, strict=True)
        ):
            self.index.upsert(
                namespace="skill_example",
                source_id=f"{example.skill_id}:{index}",
                text=text,
                metadata={
                    "skill_id": example.skill_id,
                    "arguments": example.arguments,
                    "requires_clarification": example.requires_clarification,
                    "clarification": example.clarification,
                },
                embedding=embedding,
                embedding_model=model_name,
            )
        self._last_signature = signature
        self.index.store_signature("skills", signature_digest)


class MemorySemanticIndexer:
    def __init__(
        self,
        index: VectorIndex,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self.index = index
        self.embedding_provider = embedding_provider
        self._last_signature: tuple[Any, ...] | None = None

    async def refresh(self, memory_store: MemoryStore) -> None:
        memories = memory_store.list()
        signature = tuple(
            sorted(
                (
                    item.id,
                    item.type,
                    item.key,
                    item.value,
                    item.source,
                    item.confidence,
                    item.enabled,
                )
                for item in memories
            )
        )
        signature_digest = _signature_digest(signature, self.embedding_provider)
        if signature == self._last_signature or (
            self.index.stored_signature("memory") == signature_digest
            and self.index.namespace_count({"memory"}) == len(memories)
        ):
            self._last_signature = signature
            return
        self.index.delete_namespace("memory")
        texts = [_memory_text(item) for item in memories]
        embeddings = await self.embedding_provider.embed(texts) if texts else []
        model_name = self.embedding_provider.__class__.__name__
        for item, text, embedding in zip(memories, texts, embeddings, strict=True):
            self.index.upsert(
                namespace="memory",
                source_id=str(item.id),
                text=text,
                metadata={
                    "memory_id": item.id,
                    "type": item.type,
                    "key": item.key,
                    "confidence": item.confidence,
                },
                embedding=embedding,
                embedding_model=model_name,
            )
        self._last_signature = signature
        self.index.store_signature("memory", signature_digest)


class KnowledgeSemanticIndexer:
    def __init__(
        self,
        index: VectorIndex,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self.index = index
        self.embedding_provider = embedding_provider
        self._last_signature: tuple[Any, ...] | None = None

    async def refresh(self, knowledge_store: KnowledgeBaseStore) -> None:
        knowledge_bases = tuple(knowledge_store.list())
        signature = tuple(
            sorted(
                (
                    item.id,
                    item.name,
                    item.description,
                    item.content,
                    item.source_path,
                    item.enabled,
                )
                for item in knowledge_bases
            )
        )
        chunks: list[tuple[Any, int, str]] = []
        for knowledge_base in knowledge_bases:
            if not knowledge_base.enabled:
                continue
            for index, chunk in enumerate(KnowledgeBaseStore._chunks(knowledge_base.content)):
                chunks.append((knowledge_base, index, chunk))
        signature_digest = _signature_digest(signature, self.embedding_provider)
        if signature == self._last_signature or (
            self.index.stored_signature("knowledge") == signature_digest
            and self.index.namespace_count({"knowledge"}) == len(chunks)
        ):
            self._last_signature = signature
            return
        self.index.delete_namespace("knowledge")
        texts = [_knowledge_text(item, chunk) for item, _, chunk in chunks]
        embeddings = await self.embedding_provider.embed(texts) if texts else []
        model_name = self.embedding_provider.__class__.__name__
        for (knowledge_base, chunk_index, _), text, embedding in zip(
            chunks,
            texts,
            embeddings,
            strict=True,
        ):
            self.index.upsert(
                namespace="knowledge",
                source_id=f"{knowledge_base.id}:{chunk_index}",
                text=text,
                metadata={
                    "knowledge_base_id": knowledge_base.id,
                    "name": knowledge_base.name,
                    "description": knowledge_base.description,
                    "source_path": knowledge_base.source_path,
                },
                embedding=embedding,
                embedding_model=model_name,
            )
        self._last_signature = signature
        self.index.store_signature("knowledge", signature_digest)


class WorkflowTemplateSemanticIndexer:
    def __init__(
        self,
        index: VectorIndex,
        embedding_provider: EmbeddingProvider,
        *,
        templates: tuple[WorkflowTemplate, ...] = DEFAULT_WORKFLOW_TEMPLATES,
        playbook_store: PlaybookStore | None = None,
    ) -> None:
        self.index = index
        self.embedding_provider = embedding_provider
        self.templates = templates
        self.playbook_store = playbook_store
        self._last_signature: tuple[Any, ...] | None = None

    async def refresh(self, registry: SkillRegistry | None = None) -> None:
        playbooks = self.playbook_store.list(enabled_only=True) if self.playbook_store else ()
        enabled_skill_ids = (
            {
                manifest.skill_id
                for manifest in registry.list()
                if registry.is_enabled(manifest.skill_id)
            }
            if registry is not None
            else None
        )
        signature = (
            tuple(
                json.dumps(template.model_dump(mode="json"), sort_keys=True)
                for template in self.templates
            ),
            tuple(
                json.dumps(
                    playbook.model_dump(mode="json", by_alias=True, exclude={"source_path"}),
                    sort_keys=True,
                )
                for playbook in playbooks
            ),
            tuple(sorted(enabled_skill_ids)) if enabled_skill_ids is not None else None,
        )
        signature_digest = _signature_digest(signature, self.embedding_provider)
        expected_count = len(self.templates) + len(playbooks)
        if signature == self._last_signature or (
            self.index.stored_signature("workflows") == signature_digest
            and self.index.namespace_count({"workflow_template", "playbook"}) == expected_count
        ):
            self._last_signature = signature
            return
        self.index.delete_namespace("workflow_template")
        self.index.delete_namespace("playbook")
        workflow_texts = [_workflow_text(template) for template in self.templates]
        playbook_texts = [_playbook_text(playbook) for playbook in playbooks]
        texts = workflow_texts + playbook_texts
        embeddings = await self.embedding_provider.embed(texts) if texts else []
        workflow_embeddings = embeddings[: len(workflow_texts)]
        playbook_embeddings = embeddings[len(workflow_texts) :]
        model_name = self.embedding_provider.__class__.__name__
        for template, text, embedding in zip(
            self.templates,
            workflow_texts,
            workflow_embeddings,
            strict=True,
        ):
            available_skill_ids = tuple(
                skill_id
                for skill_id in template.skill_ids
                if enabled_skill_ids is None or skill_id in enabled_skill_ids
            )
            available_completion_skill_ids = tuple(
                skill_id
                for skill_id in template.completion_skill_ids
                if skill_id in available_skill_ids
            )
            available_skill_stages = tuple(
                tuple(skill_id for skill_id in stage if skill_id in available_skill_ids)
                for stage in template.skill_stages
            )
            available_skill_stages = tuple(stage for stage in available_skill_stages if stage)
            self.index.upsert(
                namespace="workflow_template",
                source_id=template.template_id,
                text=text,
                metadata={
                    "template_id": template.template_id,
                    "title": template.title,
                    "clarification": template.clarification,
                    "triggers": list(template.triggers),
                    "skill_ids": list(available_skill_ids),
                    "skill_stages": [list(stage) for stage in available_skill_stages],
                    "completion_skill_ids": list(available_completion_skill_ids),
                },
                embedding=embedding,
                embedding_model=model_name,
            )
        for playbook, text, embedding in zip(
            playbooks,
            playbook_texts,
            playbook_embeddings,
            strict=True,
        ):
            self.index.upsert(
                namespace="playbook",
                source_id=playbook.playbook_id,
                text=text,
                metadata=_playbook_metadata(playbook, enabled_skill_ids),
                embedding=embedding,
                embedding_model=model_name,
            )
        self._last_signature = signature
        self.index.store_signature("workflows", signature_digest)


def _skill_text(manifest) -> str:
    permissions = ", ".join(permission.capability for permission in manifest.permissions)
    schema_summary = json.dumps(manifest.input_schema, sort_keys=True)
    return (
        f"Skill ID: {manifest.skill_id}\n"
        f"Name: {manifest.display_name}\n"
        f"Description: {manifest.description}\n"
        f"Risk: {manifest.risk.value}\n"
        f"Permissions: {permissions}\n"
        f"Input schema: {schema_summary}"
    )


def _example_text(example: SkillExample) -> str:
    return (
        f"Utterance: {example.utterance}\n"
        f"Skill: {example.skill_id}\n"
        f"Arguments: {json.dumps(example.arguments, sort_keys=True)}\n"
        f"Requires clarification: {example.requires_clarification}\n"
        f"Clarification: {example.clarification or ''}\n"
        f"Notes: {example.notes}"
    )


def _memory_text(item: MemoryItem) -> str:
    return (
        f"Memory type: {item.type}\n"
        f"Key: {item.key}\n"
        f"Value: {item.value}\n"
        f"Source: {item.source}\n"
        f"Confidence: {item.confidence}"
    )


def _knowledge_text(item, chunk: str) -> str:
    return f"Knowledge base: {item.name}\nDescription: {item.description}\nContent: {chunk}"


def _workflow_text(template: WorkflowTemplate) -> str:
    return (
        f"Workflow: {template.title}\n"
        f"Description: {template.description}\n"
        f"Triggers: {', '.join(template.triggers)}\n"
        f"Steps: {'; '.join(template.steps)}\n"
        f"Registered skills: {', '.join(template.skill_ids)}\n"
        "Skill stages: "
        f"{' -> '.join(' | '.join(stage) for stage in template.skill_stages)}\n"
        f"Completion skills: {', '.join(template.completion_skill_ids)}\n"
        f"Clarification: {template.clarification or ''}"
    )


def _playbook_text(playbook: Playbook) -> str:
    skill_guidance = "; ".join(
        f"{item.skill_id}: {item.instructions or 'No additional guidance.'}"
        for item in playbook.skills
    )
    return (
        f"User playbook ID: {playbook.playbook_id}\n"
        f"Name: {playbook.name}\n"
        f"Description: {playbook.description}\n"
        f"Triggers: {', '.join(playbook.triggers)}\n"
        f"Registered skill guidance: {skill_guidance}\n"
        f"Workflow instructions:\n{playbook.instructions}"
    )


def _playbook_metadata(
    playbook: Playbook,
    enabled_skill_ids: set[str] | None,
) -> dict[str, Any]:
    available_ids = tuple(
        skill_id
        for skill_id in playbook.skill_ids
        if enabled_skill_ids is None or skill_id in enabled_skill_ids
    )
    unavailable_ids = tuple(
        skill_id for skill_id in playbook.skill_ids if skill_id not in available_ids
    )
    return {
        "playbook_id": playbook.playbook_id,
        "name": playbook.name,
        "version": playbook.version,
        "content_hash": playbook.content_hash,
        "skill_ids": list(available_ids),
        "unavailable_skill_ids": list(unavailable_ids),
        "skill_guidance": {
            guidance.skill_id: guidance.instructions for guidance in playbook.skills
        },
        "triggers": list(playbook.triggers),
    }


def playbook_context_item(
    playbook: Playbook,
    registry: SkillRegistry,
    *,
    score: float = 1.0,
) -> RetrievedContextItem:
    enabled_skill_ids = {
        manifest.skill_id for manifest in registry.list() if registry.is_enabled(manifest.skill_id)
    }
    return RetrievedContextItem(
        namespace="playbook",
        source_id=playbook.playbook_id,
        text=_playbook_text(playbook),
        metadata=_playbook_metadata(playbook, enabled_skill_ids),
        score=score,
    )
