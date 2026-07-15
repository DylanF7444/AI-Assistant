from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from assistant.contracts import ResultStatus, SkillCall, SkillResult
from assistant.scheduling.store import ScheduleStore
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.storage.database import Database, ItemRow, now


class TaskInput(BaseModel):
    operation: Literal["add", "list", "complete", "delete"]
    title: str | None = None
    item_id: int | None = None


class TaskOutput(BaseModel):
    items: list[dict]


class TaskSkill(Skill):
    input_model = TaskInput
    output_model = TaskOutput

    def __init__(self, database: Database) -> None:
        self.database = database
        self.manifest = manifest(
            skill_id="core.tasks",
            display_name="Tasks",
            description="Manage a local task list.",
            input_model=self.input_model,
            output_model=self.output_model,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        changed_title: str | None = None
        with self.database.session() as session:
            if args.operation == "add":
                if not args.title:
                    raise ValueError("A task title is required")
                session.add(ItemRow(kind="task", title=args.title, body="", created_at=now()))
                changed_title = args.title
            elif args.operation in {"complete", "delete"}:
                if args.item_id is None:
                    raise ValueError("An item ID is required")
                row = session.get(ItemRow, args.item_id)
                if row is None or row.kind != "task":
                    raise ValueError("Task not found")
                changed_title = row.title
                if args.operation == "complete":
                    row.completed = True
                else:
                    session.delete(row)
            rows = list(
                session.scalars(
                    select(ItemRow)
                    .where(ItemRow.kind == "task")
                    .order_by(ItemRow.completed, ItemRow.id)
                )
            )
            values = [
                {"id": row.id, "title": row.title, "completed": row.completed} for row in rows
            ]
        display = _task_display(args.operation, values, changed_title)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"{len(values)} tasks in the local list.",
            display_text=display,
            data=TaskOutput(items=values).model_dump(),
        )


class NoteInput(BaseModel):
    operation: Literal["add", "search", "delete"]
    title: str | None = None
    body: str | None = None
    query: str | None = None
    item_id: int | None = None


class NoteOutput(BaseModel):
    notes: list[dict]


class NoteSkill(Skill):
    input_model = NoteInput
    output_model = NoteOutput

    def __init__(self, database: Database) -> None:
        self.database = database
        self.manifest = manifest(
            skill_id="core.notes",
            display_name="Notes",
            description="Store and search local notes and knowledge entries.",
            input_model=self.input_model,
            output_model=self.output_model,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        changed_title: str | None = None
        with self.database.session() as session:
            if args.operation == "add":
                if not args.title or not args.body:
                    raise ValueError("A note title and body are required")
                session.add(
                    ItemRow(
                        kind="note",
                        title=args.title,
                        body=args.body,
                        created_at=now(),
                    )
                )
                session.flush()
                changed_title = args.title
            elif args.operation == "delete":
                row = session.get(ItemRow, args.item_id)
                if row is None or row.kind != "note":
                    raise ValueError("Note not found")
                changed_title = row.title
                session.delete(row)
            statement = select(ItemRow).where(ItemRow.kind == "note")
            rows = list(session.scalars(statement.order_by(ItemRow.id.desc())))
            if args.operation == "search" and args.query:
                needle = args.query.casefold()
                rows = [
                    row
                    for row in rows
                    if needle in row.title.casefold() or needle in row.body.casefold()
                ]
            values = [{"id": row.id, "title": row.title, "body": row.body} for row in rows]
        display = _note_display(args.operation, values, changed_title)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Found {len(values)} local notes.",
            display_text=display,
            data=NoteOutput(notes=values).model_dump(),
            sensitive=True,
        )


class ReminderInput(BaseModel):
    title: str
    due_at: datetime | None = None
    delay_seconds: float | None = Field(default=None, gt=0, le=31_536_000)

    @model_validator(mode="after")
    def _requires_one_time_source(self) -> ReminderInput:
        if (self.due_at is None) == (self.delay_seconds is None):
            raise ValueError("Provide exactly one of due_at or delay_seconds")
        return self


class ReminderOutput(BaseModel):
    schedule_id: str
    due_at: datetime
    status: str


class ReminderSkill(Skill):
    input_model = ReminderInput
    output_model = ReminderOutput

    def __init__(self, store: ScheduleStore | Database) -> None:
        self.store = store if isinstance(store, ScheduleStore) else ScheduleStore(store)
        self.manifest = manifest(
            skill_id="core.reminder",
            display_name="Reminder",
            description="Create a local reminder or alarm.",
            input_model=self.input_model,
            output_model=self.output_model,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        due = (
            self.store.clock.now() + timedelta(seconds=args.delay_seconds)
            if args.delay_seconds is not None
            else args.due_at
        )
        assert due is not None
        if due.tzinfo is None:
            due = due.astimezone()
        if due <= datetime.now(UTC).astimezone(due.tzinfo):
            raise ValueError("Reminder time must be in the future")
        schedule = self.store.create_notification(
            name=args.title,
            message=args.title,
            run_at=due,
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Reminder set for {due.strftime('%c')}.",
            data=ReminderOutput(
                schedule_id=str(schedule.schedule_id),
                due_at=due,
                status=schedule.status.value,
            ).model_dump(mode="json"),
        )


class ConvertInput(BaseModel):
    value: float
    from_unit: str
    to_unit: str


class ConvertOutput(BaseModel):
    value: float
    unit: str


class UnitConversionSkill(Skill):
    input_model = ConvertInput
    output_model = ConvertOutput
    _FACTORS = {
        ("kilometer", "mile"): 0.621371,
        ("mile", "kilometer"): 1.609344,
        ("meter", "foot"): 3.28084,
        ("foot", "meter"): 0.3048,
        ("kilogram", "pound"): 2.2046226218,
        ("pound", "kilogram"): 0.45359237,
        ("liter", "gallon"): 0.2641720524,
        ("gallon", "liter"): 3.785411784,
    }
    manifest = manifest(
        skill_id="core.unit_conversion",
        display_name="Unit Conversion",
        description="Convert common units locally.",
        input_model=input_model,
        output_model=output_model,
        safe_to_retry=True,
    )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        source = _normalize_unit(args.from_unit)
        target = _normalize_unit(args.to_unit)
        if (source, target) == ("celsius", "fahrenheit"):
            value = args.value * 9 / 5 + 32
        elif (source, target) == ("fahrenheit", "celsius"):
            value = (args.value - 32) * 5 / 9
        else:
            try:
                value = args.value * self._FACTORS[(source, target)]
            except KeyError as exc:
                raise ValueError("Unsupported unit conversion") from exc
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"{args.value:g} {args.from_unit} is {value:g} {args.to_unit}.",
            data=ConvertOutput(value=value, unit=args.to_unit).model_dump(),
        )


def _task_display(
    operation: str,
    items: list[dict],
    changed_title: str | None,
) -> str:
    if operation == "add" and changed_title:
        heading = f'Added task: "{changed_title}".'
    elif operation == "complete" and changed_title:
        heading = f'Completed task: "{changed_title}".'
    elif operation == "delete" and changed_title:
        heading = f'Deleted task: "{changed_title}".'
    else:
        heading = "Tasks:"
    if not items:
        return heading if operation == "delete" else "There are no tasks in the local list."
    lines = [
        f"- [{'x' if item['completed'] else ' '}] {item['id']}: {item['title']}" for item in items
    ]
    return heading + "\n" + "\n".join(lines)


def _note_display(
    operation: str,
    notes: list[dict],
    changed_title: str | None,
) -> str:
    if operation == "add" and changed_title:
        return f'Saved note: "{changed_title}".'
    if operation == "delete" and changed_title:
        return f'Deleted note: "{changed_title}".'
    if not notes:
        return "No matching local notes were found."
    return "Matching notes:\n" + "\n".join(
        f"- {note['id']}: {note['title']} — {note['body']}" for note in notes
    )


def _normalize_unit(value: str) -> str:
    normalized = value.strip().casefold()
    aliases = {
        "feet": "foot",
        "celsius": "celsius",
        "fahrenheit": "fahrenheit",
    }
    if normalized in aliases:
        return aliases[normalized]
    return normalized[:-1] if normalized.endswith("s") else normalized
