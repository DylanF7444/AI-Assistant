from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bootstrap import build_container
from assistant.contracts import PermissionContext, RiskLevel, SkillCall
from assistant.language.embeddings import LexicalFallbackEmbeddingProvider
from assistant.language.retrieval import (
    SemanticRetriever,
    WorkflowTemplateSemanticIndexer,
)
from assistant.skills.builtin import CalculatorSkill
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import Database
from assistant.storage.playbooks import PlaybookStore
from assistant.storage.vector_index import VectorIndex

PLAYBOOK_MARKDOWN = """---
id: monthly-report
name: Monthly report
description: Prepare the recurring project status report.
version: 2
enabled: true
triggers:
  - prepare my monthly report
skills:
  - id: core.calculator
    instructions: Calculate totals only after collecting all supplied values.
  - id: missing.publisher
    instructions: Publish only when that separately installed skill is available.
---

# Workflow

1. Gather the reporting period and source values.
2. Calculate totals with the registered calculator skill.
3. Present a draft before any publishing step.
"""


def test_playbook_store_round_trips_markdown_and_enabled_state(tmp_path: Path):
    store = PlaybookStore(tmp_path / "playbooks")

    playbook = store.save_markdown(PLAYBOOK_MARKDOWN)

    assert playbook.playbook_id == "monthly-report"
    assert playbook.version == "2"
    assert playbook.skill_ids == ("core.calculator", "missing.publisher")
    assert playbook.skills[0].instructions.startswith("Calculate totals")
    assert playbook.content_hash
    assert playbook.source_path == (tmp_path / "playbooks" / "monthly-report.md").resolve()

    disabled = store.set_enabled("monthly-report", False)
    assert disabled.enabled is False
    assert store.list(enabled_only=True) == ()

    edited = store.save_markdown(
        PLAYBOOK_MARKDOWN.replace("monthly-report", "status-report"),
        original_id="monthly-report",
    )
    assert edited.playbook_id == "status-report"
    assert not (tmp_path / "playbooks" / "monthly-report.md").exists()
    assert store.delete("status-report") is True
    assert store.list() == ()


def test_playbook_store_rejects_invalid_or_duplicate_skill_guidance(tmp_path: Path):
    store = PlaybookStore(tmp_path / "playbooks")
    duplicate = PLAYBOOK_MARKDOWN.replace(
        "  - id: missing.publisher",
        "  - id: core.calculator",
    )

    with pytest.raises(ValueError, match="same skill ID"):
        store.save_markdown(duplicate)

    recursive = PLAYBOOK_MARKDOWN.replace("core.calculator", "core.playbook").replace(
        "  - id: missing.publisher\n"
        "    instructions: Publish only when that separately installed skill is available.\n",
        "",
    )
    with pytest.raises(ValueError, match="cannot invoke playbook management"):
        store.save_markdown(recursive)

    invalid_path = store.root / "broken.md"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_text("# Missing frontmatter", encoding="utf-8")
    assert "broken.md" in next(iter(store.errors()))


@pytest.mark.asyncio
async def test_enabled_playbook_is_indexed_with_only_available_skill_ids(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    index = VectorIndex(database)
    provider = LexicalFallbackEmbeddingProvider()
    store = PlaybookStore(tmp_path / "playbooks")
    store.save_markdown(PLAYBOOK_MARKDOWN)
    registry = SkillRegistry(require_signatures=False)
    registry.register(CalculatorSkill())
    indexer = WorkflowTemplateSemanticIndexer(
        index,
        provider,
        playbook_store=store,
    )

    await indexer.refresh(registry)
    results = await SemanticRetriever(index, provider).search(
        "prepare my monthly report",
        namespaces={"playbook"},
        limit=3,
    )

    assert results
    assert results[0].metadata["playbook_id"] == "monthly-report"
    assert results[0].metadata["skill_ids"] == ["core.calculator"]
    assert results[0].metadata["unavailable_skill_ids"] == ["missing.publisher"]
    assert "Calculate totals only" in results[0].text

    store.set_enabled("monthly-report", False)
    await indexer.refresh(registry)
    assert (
        await SemanticRetriever(index, provider).search(
            "monthly report",
            namespaces={"playbook"},
            limit=3,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_container_retrieves_enabled_playbooks_for_planner_context(settings):
    container = build_container(settings)
    container.playbook_store.save_markdown(PLAYBOOK_MARKDOWN)

    retrieved = await container.orchestrator._retrieve_planner_goal_context(
        "prepare my monthly report"
    )

    playbook = next(item for item in retrieved if item.namespace == "playbook")
    assert playbook.metadata["playbook_id"] == "monthly-report"
    assert playbook.metadata["skill_ids"] == ["core.calculator"]


@pytest.mark.asyncio
async def test_user_commands_save_list_show_and_update_playbooks(settings):
    container = build_container(settings)

    saved_response, saved_outcome = await container.orchestrator.handle_text(
        "Save a playbook called Release Check using skills core.datetime, core.tasks: "
        "First inspect the release notes. Then create a final checklist."
    )

    assert saved_outcome.result is not None
    assert saved_outcome.result.status.value == "completed"
    assert "Saved playbook" in saved_response.display_text
    playbook = container.playbook_store.get("release-check")
    assert playbook.name == "Release Check"
    assert playbook.skill_ids == ("core.datetime", "core.tasks")
    assert playbook.instructions.startswith("First inspect")

    listed_response, _ = await container.orchestrator.handle_text("list playbooks")
    shown_response, _ = await container.orchestrator.handle_text("recall playbook release-check")
    assert "Release Check (enabled)" in listed_response.display_text
    assert "First inspect the release notes" in shown_response.display_text

    updated_response, _ = await container.orchestrator.handle_text(
        "Save playbook Release Check: Finish with a concise go or no-go recommendation."
    )
    updated = container.playbook_store.get("release-check")
    assert "Saved playbook" in updated_response.display_text
    assert updated.version == "2"
    assert updated.skill_ids == ("core.datetime", "core.tasks")
    assert updated.instructions.startswith("Finish with")

    delete_response, delete_outcome = await container.orchestrator.handle_text(
        "delete playbook release-check"
    )
    assert delete_outcome.confirmation is not None
    assert "Delete playbook" in delete_response.display_text
    assert container.playbook_store.get("release-check") is not None


@pytest.mark.asyncio
async def test_save_command_rejects_unknown_skill_ids_and_delete_is_medium_risk(settings):
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text(
        "Save playbook Unsafe using skills missing.shell: Run the missing skill."
    )

    assert outcome.result is not None
    assert outcome.result.status.value == "denied"
    assert outcome.result.error is not None
    assert "Unknown registered skill IDs" in outcome.result.error.message
    assert "rejected" in response.display_text
    assert container.playbook_store.list() == ()

    container.playbook_store.save_markdown(container.playbook_store.EXAMPLE)
    skill = container.registry.get("core.playbook")
    delete_call = SkillCall(
        skill_id="core.playbook",
        arguments={"operation": "delete", "name": "prepare-release"},
        permission_context=PermissionContext(session_id="test"),
    )
    assert skill.risk_for(delete_call) == RiskLevel.MEDIUM
