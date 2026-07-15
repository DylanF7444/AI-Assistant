import asyncio
from datetime import datetime
from uuid import UUID

import pytest

from assistant.contracts import PermissionContext, ResultStatus, SkillCall
from assistant.skills.productivity import NoteSkill, ReminderSkill, TaskSkill, UnitConversionSkill
from assistant.storage.database import Database


def _call(skill_id: str, arguments: dict) -> SkillCall:
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


@pytest.mark.asyncio
async def test_celsius_conversion_preserves_unit_name():
    skill = UnitConversionSkill()

    result = await skill.execute(
        _call(
            "core.unit_conversion",
            {"value": 72, "from_unit": "fahrenheit", "to_unit": "celsius"},
        ),
        asyncio.Event(),
    )

    assert result.status == ResultStatus.COMPLETED
    assert result.data["value"] == pytest.approx(22.2222222)
    assert result.summary.startswith("72 fahrenheit is 22.2222 celsius")


@pytest.mark.asyncio
async def test_task_list_display_includes_titles_and_status(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'tasks.db'}")
    database.migrate()
    skill = TaskSkill(database)

    added = await skill.execute(
        _call("core.tasks", {"operation": "add", "title": "Review acceptance findings"}),
        asyncio.Event(),
    )
    listed = await skill.execute(
        _call("core.tasks", {"operation": "list"}),
        asyncio.Event(),
    )

    assert 'Added task: "Review acceptance findings".' in added.display_text
    assert "- [ ] 1: Review acceptance findings" in listed.display_text


@pytest.mark.asyncio
async def test_note_search_display_includes_matching_note(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'notes.db'}")
    database.migrate()
    skill = NoteSkill(database)

    await skill.execute(
        _call(
            "core.notes",
            {"operation": "add", "title": "Filter", "body": "Model X200"},
        ),
        asyncio.Event(),
    )
    result = await skill.execute(
        _call("core.notes", {"operation": "search", "query": "X200"}),
        asyncio.Event(),
    )

    assert "Matching notes:" in result.display_text
    assert "Filter — Model X200" in result.display_text


@pytest.mark.asyncio
async def test_reminder_skill_creates_an_authorized_durable_schedule(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'reminders.db'}")
    database.migrate()
    skill = ReminderSkill(database)

    result = await skill.execute(
        _call(
            "core.reminder",
            {"title": "Check the oven", "delay_seconds": 300},
        ),
        asyncio.Event(),
    )

    schedule = skill.store.get(UUID(result.data["schedule_id"]))
    assert result.status == ResultStatus.COMPLETED
    assert schedule.name == "Check the oven"
    assert schedule.status.value == "active"
    assert schedule.next_run_at == datetime.fromisoformat(result.data["due_at"])
