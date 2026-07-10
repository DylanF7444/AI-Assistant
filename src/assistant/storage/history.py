from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from assistant.storage.database import Database, HistoryRow


class HistoryStore:
    def __init__(self, database: Database, retention_days: int = 0) -> None:
        self.database = database
        self.retention_days = retention_days

    def add(
        self,
        correlation_id: UUID,
        response_text: str,
        *,
        raw_transcript: str | None = None,
        normalized_transcript: str | None = None,
        intent_name: str | None = None,
    ) -> None:
        retained = self.retention_days > 0
        with self.database.session() as session:
            session.add(
                HistoryRow(
                    timestamp=datetime.now(UTC),
                    correlation_id=str(correlation_id),
                    raw_transcript=raw_transcript if retained else None,
                    normalized_transcript=normalized_transcript if retained else None,
                    intent_name=intent_name,
                    response_text=response_text,
                    retained=retained,
                )
            )

    def prune(self) -> None:
        with self.database.session() as session:
            if self.retention_days <= 0:
                session.query(HistoryRow).filter(HistoryRow.retained.is_(True)).delete()
                return
            cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
            session.query(HistoryRow).filter(HistoryRow.timestamp < cutoff).delete()

    def clear(self) -> None:
        with self.database.session() as session:
            session.query(HistoryRow).delete()
