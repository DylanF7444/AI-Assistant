from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from assistant.storage.database import Database, VectorItemRow, now


@dataclass(frozen=True)
class VectorSearchResult:
    namespace: str
    source_id: str
    text: str
    metadata: dict[str, Any]
    score: float


class VectorIndex:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(
        self,
        *,
        namespace: str,
        source_id: str,
        text: str,
        metadata: dict[str, Any],
        embedding: list[float],
        embedding_model: str,
    ) -> None:
        namespace = namespace.strip()
        source_id = source_id.strip()
        text = text.strip()
        if not namespace:
            raise ValueError("namespace is required")
        if not source_id:
            raise ValueError("source_id is required")
        if not text:
            raise ValueError("text is required")
        if not embedding:
            raise ValueError("embedding is required")
        timestamp = now()
        payload = {
            "text": text,
            "metadata_json": json.dumps(metadata, sort_keys=True),
            "embedding_blob": _pack_vector(embedding),
            "embedding_model": embedding_model,
            "updated_at": timestamp,
        }
        with self.database.session() as session:
            row = session.scalar(
                select(VectorItemRow).where(
                    VectorItemRow.namespace == namespace,
                    VectorItemRow.source_id == source_id,
                )
            )
            if row is None:
                session.add(
                    VectorItemRow(
                        namespace=namespace,
                        source_id=source_id,
                        **payload,
                    )
                )
            else:
                for key, value in payload.items():
                    setattr(row, key, value)

    def delete_namespace(self, namespace: str) -> None:
        with self.database.session() as session:
            session.query(VectorItemRow).filter(VectorItemRow.namespace == namespace).delete()

    def search(
        self,
        query_embedding: list[float],
        *,
        namespaces: set[str],
        limit: int = 8,
    ) -> tuple[VectorSearchResult, ...]:
        if not query_embedding or limit <= 0 or not namespaces:
            return ()
        query_norm = _norm(query_embedding)
        if query_norm == 0:
            return ()
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(VectorItemRow).where(VectorItemRow.namespace.in_(sorted(namespaces)))
                )
            )
        scored: list[VectorSearchResult] = []
        for row in rows:
            if row.embedding_blob is None:
                continue
            embedding = _unpack_vector(row.embedding_blob)
            if len(embedding) != len(query_embedding):
                continue
            score = _cosine(query_embedding, query_norm, embedding)
            scored.append(
                VectorSearchResult(
                    namespace=row.namespace,
                    source_id=row.source_id,
                    text=row.text,
                    metadata=json.loads(row.metadata_json or "{}"),
                    score=score,
                )
            )
        return tuple(
            sorted(
                scored,
                key=lambda item: (-item.score, item.namespace, item.source_id),
            )[:limit]
        )


def _pack_vector(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}d", *[float(value) for value in values])


def _unpack_vector(payload: bytes) -> list[float]:
    if len(payload) % 8:
        raise ValueError("invalid vector payload")
    return list(struct.unpack(f"<{len(payload) // 8}d", payload))


def _norm(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def _cosine(query: list[float], query_norm: float, candidate: list[float]) -> float:
    candidate_norm = _norm(candidate)
    if candidate_norm == 0:
        return 0.0
    dot = sum(left * right for left, right in zip(query, candidate, strict=True))
    return dot / (query_norm * candidate_norm)
