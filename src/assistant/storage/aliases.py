from sqlalchemy import select

from assistant.storage.database import AliasRow, Database


class AliasStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def set(self, kind: str, spoken: str, canonical: str) -> None:
        spoken = spoken.casefold().strip()
        with self.database.session() as session:
            row = session.get(AliasRow, (kind, spoken))
            if row is None:
                session.add(AliasRow(kind=kind, spoken=spoken, canonical=canonical))
            else:
                row.canonical = canonical

    def set_many(self, kind: str, aliases: dict[str, str]) -> tuple[int, int]:
        added = 0
        updated = 0
        normalized = {
            spoken.casefold().strip(): canonical
            for spoken, canonical in aliases.items()
            if spoken.strip()
        }
        if not normalized:
            return added, updated
        with self.database.session() as session:
            for spoken, canonical in normalized.items():
                row = session.get(AliasRow, (kind, spoken))
                if row is None:
                    session.add(AliasRow(kind=kind, spoken=spoken, canonical=canonical))
                    added += 1
                elif row.canonical != canonical:
                    row.canonical = canonical
                    updated += 1
        return added, updated

    def resolve(self, kind: str, spoken: str) -> str:
        with self.database.session() as session:
            row = session.get(AliasRow, (kind, spoken.casefold().strip()))
            return row.canonical if row else spoken

    def get(self, kind: str, spoken: str) -> str | None:
        with self.database.session() as session:
            row = session.get(AliasRow, (kind, spoken.casefold().strip()))
            return row.canonical if row else None

    def delete(self, kind: str, spoken: str) -> bool:
        with self.database.session() as session:
            row = session.get(AliasRow, (kind, spoken.casefold().strip()))
            if row is None:
                return False
            session.delete(row)
            return True

    def all(self, kind: str) -> dict[str, str]:
        with self.database.session() as session:
            rows = session.scalars(select(AliasRow).where(AliasRow.kind == kind))
            return {row.spoken: row.canonical for row in rows}
