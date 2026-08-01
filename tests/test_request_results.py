import pytest

from assistant.bootstrap import build_container
from assistant.contracts import RequestResultItem, ResponseType
from assistant.results import contextual_result_selector, select_result_item


def _task_items():
    return (
        RequestResultItem(
            item_id="task-1",
            title="Review documentation",
            value={"id": 1},
            kind="task",
        ),
        RequestResultItem(
            item_id="task-2",
            title="Ship the release",
            value={"id": 2},
            kind="task",
        ),
    )


@pytest.mark.parametrize(
    ("selector", "item_id"),
    [
        ("second", "task-2"),
        ("item 2", "task-2"),
        ("task-2", "task-2"),
        ("#task-2", "task-2"),
        ("Ship the release", "task-2"),
        ("last", "task-2"),
    ],
)
def test_generic_result_selection_resolves_ordinals_ids_and_titles(selector, item_id):
    selection = select_result_item(_task_items(), selector)

    assert selection.failure is None
    assert selection.item.item_id == item_id


@pytest.mark.parametrize(
    ("text", "selector"),
    [
        ("Tell me more about the first one", "first"),
        ("Open article number 2", "2"),
        ("Use the last result", "last"),
        ("What is the 3rd option?", "3rd"),
    ],
)
def test_contextual_result_selector_extracts_explicit_result_references(text, selector):
    assert contextual_result_selector(text) == selector


def test_contextual_result_selector_ignores_unrelated_ordinals():
    assert contextual_result_selector("Who was the first president?") is None


@pytest.mark.asyncio
async def test_orchestrator_selects_non_file_result_and_exposes_it_to_context(settings):
    container = build_container(settings)
    items = _task_items()
    container.orchestrator.remember_result_items(
        items,
        source="core.tasks",
        summary="Two tasks.",
    )

    response, _ = await container.orchestrator.handle_text("select the second one")
    context = await container.orchestrator._context_snapshot_for_planner_goal(
        "Complete it.",
        session_id="local",
    )

    assert response.response_type == ResponseType.STATUS
    assert response.selected_item_id == "task-2"
    assert response.items == (items[1],)
    assert container.orchestrator.state.selected_result_item == items[1]
    selected_context = context.items_by_key()["results.selected"].value
    assert selected_context["source"] == "core.tasks"
    assert selected_context["value"] == {"id": 2}
