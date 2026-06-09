from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from assistant.contracts import ResultStatus, SkillCall, SkillResult
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
        with self.database.session() as session:
            if args.operation == "add":
                if not args.title:
                    raise ValueError("A task title is required")
                session.add(
                    ItemRow(kind="task", title=args.title, body="", created_at=now())
                )
            elif args.operation in {"complete", "delete"}:
                if args.item_id is None:
                    raise ValueError("An item ID is required")
                row = session.get(ItemRow, args.item_id)
                if row is None or row.kind != "task":
                    raise ValueError("Task not found")
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
                {"id": row.id, "title": row.title, "completed": row.completed}
                for row in rows
            ]
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"{len(values)} tasks in the local list.",
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
            elif args.operation == "delete":
                row = session.get(ItemRow, args.item_id)
                if row is None or row.kind != "note":
                    raise ValueError("Note not found")
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
            values = [
                {"id": row.id, "title": row.title, "body": row.body} for row in rows
            ]
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Found {len(values)} local notes.",
            data=NoteOutput(notes=values).model_dump(),
            sensitive=True,
        )


class ReminderInput(BaseModel):
    title: str
    due_at: datetime


class ReminderOutput(BaseModel):
    item_id: int
    due_at: datetime


class ReminderSkill(Skill):
    input_model = ReminderInput
    output_model = ReminderOutput

    def __init__(self, database: Database) -> None:
        self.database = database
        self.manifest = manifest(
            skill_id="core.reminder",
            display_name="Reminder",
            description="Create a local reminder or alarm.",
            input_model=self.input_model,
            output_model=self.output_model,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        due = args.due_at
        if due.tzinfo is None:
            due = due.astimezone()
        if due <= datetime.now(UTC).astimezone(due.tzinfo):
            raise ValueError("Reminder time must be in the future")
        with self.database.session() as session:
            row = ItemRow(
                kind="reminder",
                title=args.title,
                body="",
                due_at=due,
                created_at=now(),
            )
            session.add(row)
            session.flush()
            item_id = row.id
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Reminder set for {due.strftime('%c')}.",
            data=ReminderOutput(item_id=item_id, due_at=due).model_dump(mode="json"),
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
        source = args.from_unit.rstrip("s").casefold()
        target = args.to_unit.rstrip("s").casefold()
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

