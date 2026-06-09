from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class SchemaVersionRow(Base):
    __tablename__ = "schema_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class SettingRow(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PermissionRow(Base):
    __tablename__ = "permissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(200), index=True)
    skill_id: Mapped[str] = mapped_column(String(200), index=True)
    capability: Mapped[str] = mapped_column(String(200), index=True)
    effect: Mapped[str] = mapped_column(String(30))
    grant_type: Mapped[str] = mapped_column(String(30))
    targets_json: Mapped[str] = mapped_column(Text, default="[]")
    session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "skill_id",
            "capability",
            "grant_type",
            "session_id",
            name="uq_permission",
        ),
    )


class AuditRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    skill_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    decision: Mapped[str | None] = mapped_column(String(50), nullable=True)
    permissions_json: Mapped[str] = mapped_column(Text, default="[]")
    details_json: Mapped[str] = mapped_column(Text, default="{}")


class HistoryRow(Base):
    __tablename__ = "history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    raw_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    response_text: Mapped[str] = mapped_column(Text)
    retained: Mapped[bool] = mapped_column(Boolean, default=False)


class SkillStateRow(Base):
    __tablename__ = "skill_state"
    skill_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    version: Mapped[str] = mapped_column(String(50))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    manifest_json: Mapped[str] = mapped_column(Text)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AliasRow(Base):
    __tablename__ = "aliases"
    kind: Mapped[str] = mapped_column(String(50), primary_key=True)
    spoken: Mapped[str] = mapped_column(String(200), primary_key=True)
    canonical: Mapped[str] = mapped_column(String(500))


class ItemRow(Base):
    __tablename__ = "local_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MacroRow(Base):
    __tablename__ = "macros"
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    steps_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MetricRow(Base):
    __tablename__ = "pipeline_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    stage: Mapped[str] = mapped_column(String(100), index=True)
    duration_ms: Mapped[float | None] = mapped_column(nullable=True)
    value: Mapped[float | None] = mapped_column(nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(100), nullable=True)


class Database:
    CURRENT_SCHEMA_VERSION = 3

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.close()

    def migrate(self) -> None:
        try:
            Base.metadata.create_all(self.engine)
        except OperationalError as exc:
            # A second desktop/CLI process may finish the same idempotent DDL
            # between SQLite's table check and CREATE TABLE.
            if "already exists" not in str(exc):
                raise
            Base.metadata.create_all(self.engine)
        with self.session() as session:
            row = session.get(SchemaVersionRow, 1)
            if row is None:
                session.add(
                    SchemaVersionRow(id=1, version=self.CURRENT_SCHEMA_VERSION)
                )
            elif row.version > self.CURRENT_SCHEMA_VERSION:
                raise RuntimeError("Database was created by a newer application version")
            elif row.version < self.CURRENT_SCHEMA_VERSION:
                self._run_migrations(session, row.version)

    def _run_migrations(self, session: Session, from_version: int) -> None:
        version = from_version
        while version < self.CURRENT_SCHEMA_VERSION:
            if version == 1:
                MacroRow.__table__.create(self.engine, checkfirst=True)
            if version == 2:
                MetricRow.__table__.create(self.engine, checkfirst=True)
            version += 1
        session.get(SchemaVersionRow, 1).version = version  # type: ignore[union-attr]

    @contextmanager
    def session(self):
        session = self.sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def integrity_check(self) -> bool:
        if self.url.startswith("sqlite"):
            import sqlite3

            path = Path(self.engine.url.database or "")
            connection = None
            try:
                connection = sqlite3.connect(
                    f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1
                )
                return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            except Exception:
                return False
            finally:
                if connection is not None:
                    connection.close()
        try:
            with self.engine.connect() as connection:
                return (
                    connection.exec_driver_sql("PRAGMA integrity_check").scalar()
                    == "ok"
                )
        except Exception:
            return False

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.connect() as connection:
            raw = connection.connection.driver_connection
            import sqlite3

            target = sqlite3.connect(destination)
            try:
                raw.backup(target)
            finally:
                target.close()

    def recover(self, backup_path: Path | None = None) -> bool:
        """Restore a SQLite backup after an integrity failure."""
        if self.integrity_check():
            return False
        if backup_path is None or not backup_path.is_file():
            raise RuntimeError("Database is corrupt and no valid backup was supplied")
        database_path = Path(self.engine.url.database or "")
        self.engine.dispose()
        corrupt_path = database_path.with_suffix(
            database_path.suffix + f".corrupt-{int(datetime.now(UTC).timestamp())}"
        )
        database_path.replace(corrupt_path)
        import sqlite3

        source = sqlite3.connect(backup_path)
        destination = sqlite3.connect(database_path)
        try:
            source.backup(destination)
        finally:
            source.close()
            destination.close()
        if not self.integrity_check():
            raise RuntimeError("Restored database failed its integrity check")
        return True


def now() -> datetime:
    return datetime.now(UTC)


class SettingsStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, key: str, default: Any = None) -> Any:
        with self.database.session() as session:
            row = session.get(SettingRow, key)
            return default if row is None else json.loads(row.value_json)

    def set(self, key: str, value: Any) -> None:
        with self.database.session() as session:
            row = session.get(SettingRow, key)
            payload = json.dumps(value)
            if row is None:
                session.add(SettingRow(key=key, value_json=payload, updated_at=now()))
            else:
                row.value_json = payload
                row.updated_at = now()


class AuditStore:
    SENSITIVE_KEYS = {
        "password",
        "token",
        "secret",
        "api_key",
        "pin",
        "message_body",
        "clipboard",
        "text",
    }

    def __init__(self, database: Database) -> None:
        self.database = database

    @classmethod
    def redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if key.lower() in cls.SENSITIVE_KEYS else cls.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls.redact(item) for item in value]
        return value

    def record(
        self,
        *,
        correlation_id: str,
        event_type: str,
        skill_id: str | None = None,
        decision: str | None = None,
        permissions: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.database.session() as session:
            session.add(
                AuditRow(
                    timestamp=now(),
                    correlation_id=correlation_id,
                    event_type=event_type,
                    skill_id=skill_id,
                    decision=decision,
                    permissions_json=json.dumps(permissions or []),
                    details_json=json.dumps(self.redact(details or {}), default=str),
                )
            )

    def query(
        self, *, event_type: str | None = None, skill_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            statement = select(AuditRow).order_by(AuditRow.timestamp.desc())
            if event_type:
                statement = statement.where(AuditRow.event_type == event_type)
            if skill_id:
                statement = statement.where(AuditRow.skill_id == skill_id)
            return [
                {
                    "timestamp": row.timestamp,
                    "correlation_id": row.correlation_id,
                    "event_type": row.event_type,
                    "skill_id": row.skill_id,
                    "decision": row.decision,
                    "permissions": json.loads(row.permissions_json),
                    "details": json.loads(row.details_json),
                }
                for row in session.scalars(statement)
            ]

    def clear(self) -> None:
        with self.database.session() as session:
            session.query(AuditRow).delete()


class MetricsStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(
        self,
        *,
        correlation_id: str,
        stage: str,
        duration_ms: float | None = None,
        value: float | None = None,
        outcome: str | None = None,
    ) -> None:
        with self.database.session() as session:
            session.add(
                MetricRow(
                    timestamp=now(),
                    correlation_id=correlation_id,
                    stage=stage,
                    duration_ms=duration_ms,
                    value=value,
                    outcome=outcome,
                )
            )

    def recent(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.scalars(
                select(MetricRow).order_by(MetricRow.id.desc()).limit(limit)
            )
            return [
                {
                    "timestamp": row.timestamp,
                    "correlation_id": row.correlation_id,
                    "stage": row.stage,
                    "duration_ms": row.duration_ms,
                    "value": row.value,
                    "outcome": row.outcome,
                }
                for row in rows
            ]

    def clear(self) -> None:
        with self.database.session() as session:
            session.query(MetricRow).delete()
