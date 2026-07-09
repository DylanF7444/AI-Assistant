from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select

from assistant.storage.database import Database, MemoryItemRow, now

MemoryType = Literal[
    "preference",
    "project",
    "identity",
    "workflow",
    "correction",
    "constraint",
]


@dataclass(frozen=True)
class MemoryItem:
    id: int
    type: str
    key: str
    value: str
    source: str
    confidence: float
    enabled: bool


class MemoryStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def remember(
        self,
        *,
        type: MemoryType,
        key: str,
        value: str,
        source: str,
        confidence: float,
    ) -> MemoryItem:
        key = _clean_key(key)
        value = " ".join(value.split())
        if not value:
            raise ValueError("memory value is required")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        timestamp = now()
        with self.database.session() as session:
            row = session.scalar(
                select(MemoryItemRow).where(
                    MemoryItemRow.type == type,
                    MemoryItemRow.key == key,
                )
            )
            if row is None:
                row = MemoryItemRow(
                    type=type,
                    key=key,
                    value=value,
                    source=source,
                    confidence=confidence,
                    enabled=True,
                    created_at=timestamp,
                    updated_at=timestamp,
                    last_used_at=None,
                )
                session.add(row)
                session.flush()
            else:
                row.value = value
                row.source = source
                row.confidence = confidence
                row.enabled = True
                row.updated_at = timestamp
            return _value(row)

    def list(
        self,
        *,
        type: str | None = None,
        include_disabled: bool = False,
    ) -> tuple[MemoryItem, ...]:
        with self.database.session() as session:
            statement = select(MemoryItemRow).order_by(
                MemoryItemRow.type,
                MemoryItemRow.key,
            )
            if type:
                statement = statement.where(MemoryItemRow.type == type)
            if not include_disabled:
                statement = statement.where(MemoryItemRow.enabled)
            return tuple(_value(row) for row in session.scalars(statement))

    def get(self, memory_id: int) -> MemoryItem | None:
        with self.database.session() as session:
            row = session.get(MemoryItemRow, memory_id)
            return _value(row) if row is not None else None

    def update(
        self,
        *,
        memory_id: int | None = None,
        key: str | None = None,
        type: MemoryType | None = None,
        value: str | None = None,
        confidence: float | None = None,
        enabled: bool | None = None,
    ) -> MemoryItem:
        if memory_id is None and not key:
            raise ValueError("memory_id or key is required")
        with self.database.session() as session:
            row = (
                session.get(MemoryItemRow, memory_id)
                if memory_id is not None
                else session.scalar(
                    select(MemoryItemRow).where(MemoryItemRow.key == _clean_key(key or ""))
                )
            )
            if row is None:
                raise KeyError(memory_id if memory_id is not None else key)
            if type is not None:
                row.type = type
            if value is not None:
                cleaned_value = " ".join(value.split())
                if not cleaned_value:
                    raise ValueError("memory value is required")
                row.value = cleaned_value
            if confidence is not None:
                if not 0 <= confidence <= 1:
                    raise ValueError("confidence must be between 0 and 1")
                row.confidence = confidence
            if enabled is not None:
                row.enabled = enabled
            row.updated_at = now()
            return _value(row)

    def search(
        self,
        query: str,
        *,
        type: str | None = None,
        limit: int = 8,
    ) -> tuple[MemoryItem, ...]:
        terms = _terms(query)
        if not terms or limit <= 0:
            return ()
        scored: list[tuple[int, MemoryItem]] = []
        for item in self.list(type=type):
            haystack = _terms(f"{item.key} {item.value}")
            score = len(terms & haystack)
            if score:
                scored.append((score, item))
        selected = [
            item
            for _, item in sorted(
                scored,
                key=lambda value: (-value[0], value[1].type, value[1].key),
            )[:limit]
        ]
        if selected:
            timestamp = now()
            with self.database.session() as session:
                for item in selected:
                    row = session.get(MemoryItemRow, item.id)
                    if row is not None:
                        row.last_used_at = timestamp
        return tuple(selected)

    def forget(
        self,
        *,
        memory_id: int | None = None,
        key: str | None = None,
        query: str | None = None,
    ) -> bool:
        with self.database.session() as session:
            row = None
            if memory_id is not None:
                row = session.get(MemoryItemRow, memory_id)
            elif key:
                row = session.scalar(
                    select(MemoryItemRow).where(MemoryItemRow.key == _clean_key(key))
                )
            elif query:
                matches = self.search(query, limit=1)
                row = session.get(MemoryItemRow, matches[0].id) if matches else None
            if row is None:
                return False
            row.enabled = False
            row.updated_at = now()
            return True

    def delete(
        self,
        *,
        memory_id: int | None = None,
        key: str | None = None,
    ) -> bool:
        if memory_id is None and not key:
            raise ValueError("memory_id or key is required")
        with self.database.session() as session:
            row = (
                session.get(MemoryItemRow, memory_id)
                if memory_id is not None
                else session.scalar(
                    select(MemoryItemRow).where(MemoryItemRow.key == _clean_key(key or ""))
                )
            )
            if row is None:
                return False
            session.delete(row)
            return True


def _value(row: MemoryItemRow) -> MemoryItem:
    return MemoryItem(
        id=row.id,
        type=row.type,
        key=row.key,
        value=row.value,
        source=row.source,
        confidence=row.confidence,
        enabled=row.enabled,
    )


def _clean_key(value: str) -> str:
    key = "_".join(re.findall(r"[a-z0-9]+", value.casefold()))
    if not key:
        raise ValueError("memory key is required")
    return key[:200]


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))
