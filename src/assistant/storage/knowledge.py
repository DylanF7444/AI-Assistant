from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from assistant.storage.database import Database, KnowledgeBaseRow, now


@dataclass(frozen=True)
class KnowledgeBase:
    id: int
    name: str
    description: str
    content: str
    source_path: str | None
    enabled: bool


@dataclass(frozen=True)
class KnowledgeExcerpt:
    knowledge_base_id: int
    name: str
    text: str
    score: float


class KnowledgeBaseStore:
    SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".csv", ".json"}
    MAX_FILE_BYTES = 2 * 1024 * 1024
    MAX_CONTENT_CHARS = 500_000
    STOP_WORDS = {
        "a",
        "an",
        "and",
        "are",
        "do",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "the",
        "to",
        "what",
        "when",
        "where",
        "who",
        "why",
        "with",
        "you",
    }

    def __init__(self, database: Database) -> None:
        self.database = database

    def save(
        self,
        name: str,
        content: str,
        *,
        description: str = "",
        source_path: str | None = None,
        enabled: bool = True,
        knowledge_base_id: int | None = None,
    ) -> KnowledgeBase:
        name = name.strip()
        content = content.strip()
        if not name:
            raise ValueError("Knowledge base name is required")
        if not content:
            raise ValueError("Knowledge base content is required")
        if len(name) > 200:
            raise ValueError("Knowledge base name must be 200 characters or fewer")
        if len(description) > 500:
            raise ValueError("Description must be 500 characters or fewer")
        if len(content) > self.MAX_CONTENT_CHARS:
            raise ValueError("Knowledge base content is too large")
        with self.database.session() as session:
            row = (
                session.get(KnowledgeBaseRow, knowledge_base_id)
                if knowledge_base_id is not None
                else session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.name == name))
            )
            timestamp = now()
            if row is None:
                row = KnowledgeBaseRow(
                    name=name,
                    description=description.strip(),
                    content=content,
                    source_path=source_path,
                    enabled=enabled,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(row)
                session.flush()
            else:
                duplicate = session.scalar(
                    select(KnowledgeBaseRow).where(
                        KnowledgeBaseRow.name == name,
                        KnowledgeBaseRow.id != row.id,
                    )
                )
                if duplicate is not None:
                    raise ValueError(f'A knowledge base named "{name}" already exists')
                row.name = name
                row.description = description.strip()
                row.content = content
                row.source_path = source_path
                row.enabled = enabled
                row.updated_at = timestamp
            return self._value(row)

    def import_file(
        self,
        path: Path,
        *,
        name: str | None = None,
        description: str = "",
    ) -> KnowledgeBase:
        path = path.resolve()
        if path.suffix.casefold() not in self.SUPPORTED_SUFFIXES:
            raise ValueError("Supported knowledge files are TXT, Markdown, RST, CSV, and JSON")
        if not path.is_file():
            raise ValueError("Knowledge file does not exist")
        if path.stat().st_size > self.MAX_FILE_BYTES:
            raise ValueError("Knowledge file must be 2 MB or smaller")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Knowledge files must use UTF-8 text encoding") from exc
        return self.save(
            name or path.stem,
            content,
            description=description,
            source_path=str(path),
        )

    def get(self, knowledge_base_id: int) -> KnowledgeBase | None:
        with self.database.session() as session:
            row = session.get(KnowledgeBaseRow, knowledge_base_id)
            return self._value(row) if row else None

    def list(self) -> list[KnowledgeBase]:
        with self.database.session() as session:
            rows = session.scalars(select(KnowledgeBaseRow).order_by(KnowledgeBaseRow.name))
            return [self._value(row) for row in rows]

    def set_enabled(self, knowledge_base_id: int, enabled: bool) -> None:
        with self.database.session() as session:
            row = session.get(KnowledgeBaseRow, knowledge_base_id)
            if row is None:
                raise KeyError(knowledge_base_id)
            row.enabled = enabled
            row.updated_at = now()

    def delete(self, knowledge_base_id: int) -> None:
        with self.database.session() as session:
            row = session.get(KnowledgeBaseRow, knowledge_base_id)
            if row is not None:
                session.delete(row)

    def clear(self) -> None:
        with self.database.session() as session:
            session.query(KnowledgeBaseRow).delete()

    def search(
        self,
        query: str,
        *,
        limit: int = 4,
        max_chars: int = 5000,
    ) -> tuple[KnowledgeExcerpt, ...]:
        query_words = self._words(query) - self.STOP_WORDS
        if not query_words or limit <= 0 or max_chars <= 0:
            return ()
        query_phrase = " ".join(re.findall(r"[a-z0-9]+", query.casefold()))
        candidates: list[KnowledgeExcerpt] = []
        for knowledge_base in self.list():
            if not knowledge_base.enabled:
                continue
            metadata_words = self._words(f"{knowledge_base.name} {knowledge_base.description}")
            metadata_score = len(query_words & metadata_words) * 3
            for chunk in self._chunks(knowledge_base.content):
                chunk_words = self._words(chunk)
                overlap = query_words & chunk_words
                if not overlap and metadata_score == 0:
                    continue
                phrase_bonus = (
                    4 if len(query_phrase) >= 5 and query_phrase in chunk.casefold() else 0
                )
                density = len(overlap) / max(len(query_words), 1)
                score = metadata_score + len(overlap) * 2 + density + phrase_bonus
                candidates.append(
                    KnowledgeExcerpt(
                        knowledge_base_id=knowledge_base.id,
                        name=knowledge_base.name,
                        text=chunk,
                        score=score,
                    )
                )
        selected: list[KnowledgeExcerpt] = []
        used_chars = 0
        for excerpt in sorted(
            candidates,
            key=lambda item: (-item.score, item.name.casefold(), item.text),
        ):
            remaining = max_chars - used_chars
            if remaining <= 0 or len(selected) >= limit:
                break
            text = excerpt.text[:remaining].strip()
            if not text:
                continue
            selected.append(
                KnowledgeExcerpt(
                    excerpt.knowledge_base_id,
                    excerpt.name,
                    text,
                    excerpt.score,
                )
            )
            used_chars += len(text)
        return tuple(selected)

    @staticmethod
    def _words(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", text.casefold()))

    @staticmethod
    def _chunks(content: str, target_chars: int = 1200) -> tuple[str, ...]:
        paragraphs = [value.strip() for value in re.split(r"\n\s*\n", content) if value.strip()]
        chunks: list[str] = []
        current = ""
        for paragraph in paragraphs:
            if len(paragraph) > target_chars:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(
                    paragraph[index : index + target_chars]
                    for index in range(0, len(paragraph), target_chars)
                )
            elif not current:
                current = paragraph
            elif len(current) + len(paragraph) + 2 <= target_chars:
                current += "\n\n" + paragraph
            else:
                chunks.append(current)
                current = paragraph
        if current:
            chunks.append(current)
        return tuple(chunks)

    @staticmethod
    def _value(row: KnowledgeBaseRow) -> KnowledgeBase:
        return KnowledgeBase(
            id=row.id,
            name=row.name,
            description=row.description,
            content=row.content,
            source_path=row.source_path,
            enabled=row.enabled,
        )
