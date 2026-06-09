from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from assistant.storage.database import Database, MacroRow, now


@dataclass(frozen=True)
class MacroStep:
    skill_id: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class MacroDefinition:
    name: str
    description: str
    steps: tuple[MacroStep, ...]


class MacroStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save(self, definition: MacroDefinition) -> None:
        if not definition.name.strip():
            raise ValueError("Macro name cannot be empty")
        if not definition.steps:
            raise ValueError("Macro must contain at least one step")
        payload = json.dumps(
            [
                {"skill_id": step.skill_id, "arguments": step.arguments}
                for step in definition.steps
            ]
        )
        with self.database.session() as session:
            row = session.get(MacroRow, definition.name.casefold())
            if row is None:
                session.add(
                    MacroRow(
                        name=definition.name.casefold(),
                        description=definition.description,
                        steps_json=payload,
                        created_at=now(),
                        updated_at=now(),
                    )
                )
            else:
                row.description = definition.description
                row.steps_json = payload
                row.updated_at = now()

    def get(self, name: str) -> MacroDefinition:
        with self.database.session() as session:
            row = session.get(MacroRow, name.casefold())
            if row is None:
                raise KeyError(name)
            return MacroDefinition(
                name=row.name,
                description=row.description,
                steps=tuple(MacroStep(**item) for item in json.loads(row.steps_json)),
            )

    def list(self) -> tuple[MacroDefinition, ...]:
        with self.database.session() as session:
            rows = session.scalars(select(MacroRow).order_by(MacroRow.name))
            return tuple(
                MacroDefinition(
                    name=row.name,
                    description=row.description,
                    steps=tuple(
                        MacroStep(**item) for item in json.loads(row.steps_json)
                    ),
                )
                for row in rows
            )

    def delete(self, name: str) -> bool:
        with self.database.session() as session:
            row = session.get(MacroRow, name.casefold())
            if row is None:
                return False
            session.delete(row)
            return True

