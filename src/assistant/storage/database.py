from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from assistant.observability import (
    MetricDimensions,
    MetricDimensionValue,
    MetricStage,
    normalize_metric_dimensions,
)


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
    dimensions_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class DashboardMetricSampleRow(Base):
    __tablename__ = "dashboard_metric_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    connection_id: Mapped[str] = mapped_column(String(36), index=True)
    metric_id: Mapped[str] = mapped_column(String(200), index=True)
    series_key: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)


class KnowledgeBaseRow(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    content: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VectorItemRow(Base):
    __tablename__ = "vector_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    embedding_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "namespace",
            "source_id",
            name="uq_vector_item_namespace_source",
        ),
    )


class MemoryItemRow(Base):
    __tablename__ = "memory_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "type",
            "key",
            name="uq_memory_type_key",
        ),
    )


class ScheduleRow(Base):
    __tablename__ = "schedules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trigger_json: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    timezone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action_json: Mapped[str] = mapped_column(Text, nullable=False)
    action_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    action_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    autonomy_policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    run_policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    delivery_policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    skill_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScheduleRunRow(Base):
    __tablename__ = "schedule_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    schedule_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lateness_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    action_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LogicChoiceHistoryRow(Base):
    __tablename__ = "logic_choice_history"
    history_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    recent_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LogicWorkflowStateRow(Base):
    __tablename__ = "logic_workflow_state"
    schedule_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    condition_true_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recent_choice_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    stopped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stop_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LogicCheckpointRow(Base):
    __tablename__ = "logic_checkpoints"
    checkpoint_key: Mapped[str] = mapped_column(String(500), primary_key=True)
    option_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NotificationRow(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    schedule_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class BackgroundAuthorizationRow(Base):
    __tablename__ = "background_authorizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    schedule_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    machine_identity: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    action_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_skill_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_capabilities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_targets_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    max_risk: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "schedule_revision",
            name="uq_background_authorization_schedule_revision",
        ),
    )


class CommandCenterArtifactRow(Base):
    __tablename__ = "command_center_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    origin_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CommandCenterArtifactRevisionRow(Base):
    __tablename__ = "command_center_artifact_revisions"
    artifact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sources_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CommandCenterDeliveryRow(Base):
    __tablename__ = "command_center_deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    artifact_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_binding_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    destination_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    destination_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_preview: Mapped[str] = mapped_column(String(1000), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    result_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CommandCenterObservationCursorRow(Base):
    __tablename__ = "command_center_observation_cursors"
    schedule_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_poll_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Database:
    CURRENT_SCHEMA_VERSION = 15

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
                session.add(SchemaVersionRow(id=1, version=self.CURRENT_SCHEMA_VERSION))
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
            if version == 3:
                KnowledgeBaseRow.__table__.create(self.engine, checkfirst=True)
            if version == 4:
                VectorItemRow.__table__.create(self.engine, checkfirst=True)
            if version == 5:
                MemoryItemRow.__table__.create(self.engine, checkfirst=True)
            if version == 6:
                ScheduleRow.__table__.create(self.engine, checkfirst=True)
                ScheduleRunRow.__table__.create(self.engine, checkfirst=True)
                BackgroundAuthorizationRow.__table__.create(self.engine, checkfirst=True)
                self._migrate_legacy_reminders(session)
            if version == 7:
                columns = {
                    column["name"]
                    for column in inspect(self.engine).get_columns(
                        BackgroundAuthorizationRow.__tablename__
                    )
                }
                if "machine_identity" not in columns:
                    session.execute(
                        text(
                            "ALTER TABLE background_authorizations "
                            "ADD COLUMN machine_identity VARCHAR(64) NOT NULL DEFAULT ''"
                        )
                    )
            if version == 8:
                DashboardMetricSampleRow.__table__.create(self.engine, checkfirst=True)
            if version == 9:
                NotificationRow.__table__.create(self.engine, checkfirst=True)
            if version == 10:
                LogicChoiceHistoryRow.__table__.create(self.engine, checkfirst=True)
                LogicWorkflowStateRow.__table__.create(self.engine, checkfirst=True)
                LogicCheckpointRow.__table__.create(self.engine, checkfirst=True)
            if version == 11:
                columns = {
                    column["name"]
                    for column in inspect(self.engine).get_columns(MetricRow.__tablename__)
                }
                if "dimensions_json" not in columns:
                    session.execute(
                        text(
                            "ALTER TABLE pipeline_metrics "
                            "ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
                        )
                    )
            if version == 12:
                CommandCenterArtifactRow.__table__.create(self.engine, checkfirst=True)
                CommandCenterArtifactRevisionRow.__table__.create(
                    self.engine,
                    checkfirst=True,
                )
                CommandCenterDeliveryRow.__table__.create(self.engine, checkfirst=True)
            if version == 13:
                columns = {
                    column["name"]
                    for column in inspect(self.engine).get_columns(
                        CommandCenterArtifactRow.__tablename__
                    )
                }
                if "origin_run_id" not in columns:
                    session.execute(
                        text(
                            "ALTER TABLE command_center_artifacts "
                            "ADD COLUMN origin_run_id VARCHAR(36)"
                        )
                    )
                session.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "ix_command_center_artifacts_origin_run_id "
                        "ON command_center_artifacts (origin_run_id)"
                    )
                )
            if version == 14:
                CommandCenterObservationCursorRow.__table__.create(
                    self.engine,
                    checkfirst=True,
                )
            version += 1
        session.get(SchemaVersionRow, 1).version = version  # type: ignore[union-attr]

    @staticmethod
    def _migrate_legacy_reminders(session: Session) -> None:
        reminders = list(
            session.scalars(
                select(ItemRow).where(
                    ItemRow.kind == "reminder",
                    ItemRow.completed.is_(False),
                    ItemRow.due_at.is_not(None),
                )
            )
        )
        for reminder in reminders:
            assert reminder.due_at is not None
            due = (
                reminder.due_at.replace(tzinfo=UTC)
                if reminder.due_at.tzinfo is None
                else reminder.due_at.astimezone(UTC)
            )
            created = (
                reminder.created_at.replace(tzinfo=UTC)
                if reminder.created_at.tzinfo is None
                else reminder.created_at.astimezone(UTC)
            )
            schedule_id = str(
                uuid5(NAMESPACE_URL, f"local-assistant:legacy-reminder:{reminder.id}")
            )
            action = {
                "contract_version": "1.0",
                "kind": "notify_only",
                "title": "Reminder",
                "message": reminder.title,
            }
            policy: dict[str, Any] = {
                "contract_version": "1.0",
                "allowed_skill_ids": [],
                "allowed_capabilities": [],
                "allowed_targets": [],
                "max_risk": "low",
                "max_steps": 1,
                "max_runtime_seconds": 30,
                "max_runs_per_hour": 4,
                "max_runs_per_day": 24,
                "expires_at": None,
                "requires_unlocked_session": False,
                "allow_network": False,
                "allow_planning": False,
                "max_replans": 0,
            }
            action_payload = json.dumps(action, sort_keys=True, separators=(",", ":"))
            action_digest = hashlib.sha256(action_payload.encode("utf-8")).hexdigest()
            policy_payload = json.dumps(policy, sort_keys=True, separators=(",", ":"))
            policy_digest = hashlib.sha256(policy_payload.encode("utf-8")).hexdigest()
            if session.get(ScheduleRow, schedule_id) is None:
                session.add(
                    ScheduleRow(
                        id=schedule_id,
                        user_id="local-user",
                        name=reminder.title[:200],
                        description="Migrated local reminder",
                        status="active",
                        revision=1,
                        trigger_json=json.dumps(
                            {
                                "contract_version": "1.0",
                                "kind": "one_time",
                                "run_at": due.isoformat(),
                            }
                        ),
                        trigger_version=1,
                        timezone=None,
                        action_json=action_payload,
                        action_version=1,
                        action_digest=action_digest,
                        autonomy_policy_json=policy_payload,
                        run_policy_json="{}",
                        delivery_policy_json="{}",
                        skill_snapshot_json="[]",
                        next_run_at=due,
                        created_at=created,
                        updated_at=created,
                        authorized_at=created,
                    )
                )
                session.add(
                    BackgroundAuthorizationRow(
                        id=str(uuid4()),
                        schedule_id=schedule_id,
                        schedule_revision=1,
                        user_id="local-user",
                        action_digest=action_digest,
                        policy_digest=policy_digest,
                        allowed_skill_ids_json="[]",
                        allowed_capabilities_json="[]",
                        allowed_targets_json="[]",
                        max_risk="low",
                        created_at=created,
                    )
                )
                session.add(
                    AuditRow(
                        timestamp=now(),
                        correlation_id=str(uuid4()),
                        event_type="schedule_migrated",
                        skill_id="core.reminder",
                        decision="active",
                        permissions_json="[]",
                        details_json=json.dumps(
                            {"schedule_id": schedule_id, "legacy_item_id": reminder.id}
                        ),
                    )
                )
            reminder.completed = True

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
                connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1)
                return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            except Exception:
                return False
            finally:
                if connection is not None:
                    connection.close()
        try:
            with self.engine.connect() as connection:
                return connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
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
    URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", flags=re.IGNORECASE)

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
        if isinstance(value, tuple):
            return tuple(cls.redact(item) for item in value)
        if isinstance(value, str):
            return cls.URL_PATTERN.sub(cls._redact_url, value)
        return value

    @staticmethod
    def _redact_url(match: re.Match[str]) -> str:
        parsed = urlparse(match.group(0))
        hostname = parsed.hostname or "[redacted-host]"
        host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            host = f"{host}:{port}"
        return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))

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
        stage: str | MetricStage,
        duration_ms: float | None = None,
        value: float | None = None,
        outcome: str | None = None,
        dimensions: MetricDimensions | dict[str, MetricDimensionValue] | None = None,
    ) -> None:
        normalized_dimensions = normalize_metric_dimensions(dimensions)
        with self.database.session() as session:
            session.add(
                MetricRow(
                    timestamp=now(),
                    correlation_id=correlation_id,
                    stage=str(stage),
                    duration_ms=duration_ms,
                    value=value,
                    outcome=outcome,
                    dimensions_json=json.dumps(
                        normalized_dimensions,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )

    def recent(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.scalars(select(MetricRow).order_by(MetricRow.id.desc()).limit(limit))
            return [self._value(row) for row in rows]

    def query(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        stages: tuple[str, ...] = (),
        dimensions: dict[str, MetricDimensionValue] | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 50_000))
        with self.database.session() as session:
            statement = select(MetricRow)
            if since is not None:
                statement = statement.where(MetricRow.timestamp >= since)
            if until is not None:
                statement = statement.where(MetricRow.timestamp <= until)
            if stages:
                statement = statement.where(MetricRow.stage.in_(stages))
            rows = session.scalars(statement.order_by(MetricRow.id.desc()).limit(bounded_limit))
            values = [self._value(row) for row in rows]
            normalized_dimensions = normalize_metric_dimensions(dimensions)
            if normalized_dimensions:
                values = [
                    value
                    for value in values
                    if all(
                        value["dimensions"].get(key) == expected
                        for key, expected in normalized_dimensions.items()
                    )
                ]
            return values

    def outcome_counts(
        self,
        *,
        since: datetime,
        stage: str,
        limit: int = 5000,
    ) -> dict[str, int]:
        rows = self.query(since=since, stages=(stage,), limit=limit)
        return dict(Counter(str(row["outcome"]) for row in rows if row["outcome"]))

    def duration_summary(
        self,
        *,
        since: datetime,
        stage: str,
        dimensions: dict[str, MetricDimensionValue] | None = None,
        limit: int = 5000,
    ) -> dict[str, float | int]:
        durations = sorted(
            float(row["duration_ms"])
            for row in self.query(
                since=since,
                stages=(stage,),
                dimensions=dimensions,
                limit=limit,
            )
            if row["duration_ms"] is not None
        )
        if not durations:
            return {"count": 0, "average": 0.0, "p50": 0.0, "p95": 0.0}

        def percentile(value: float) -> float:
            index = max(0, math.ceil(value * len(durations)) - 1)
            return durations[index]

        return {
            "count": len(durations),
            "average": sum(durations) / len(durations),
            "p50": percentile(0.5),
            "p95": percentile(0.95),
        }

    def stage_summary(
        self,
        *,
        since: datetime,
        stage: str,
        dimensions: dict[str, MetricDimensionValue] | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        rows = self.query(
            since=since,
            stages=(stage,),
            dimensions=dimensions,
            limit=limit,
        )
        durations = self.duration_summary(
            since=since,
            stage=stage,
            dimensions=dimensions,
            limit=limit,
        )
        outcomes = Counter(str(row["outcome"]) for row in rows if row["outcome"])
        failed = sum(
            count
            for outcome, count in outcomes.items()
            if outcome in {"error", "failed", "timed_out", "timeout", "cancelled"}
        )
        return {
            **durations,
            "outcomes": dict(sorted(outcomes.items())),
            "failed": failed,
            "error_rate": failed / len(rows) if rows else 0.0,
        }

    def latest(self, *, stages: tuple[str, ...] = ()) -> dict[str, Any] | None:
        values = self.query(stages=stages, limit=1)
        return values[0] if values else None

    @staticmethod
    def _value(row: MetricRow) -> dict[str, Any]:
        return {
            "timestamp": row.timestamp,
            "correlation_id": row.correlation_id,
            "stage": row.stage,
            "duration_ms": row.duration_ms,
            "value": row.value,
            "outcome": row.outcome,
            "dimensions": json.loads(row.dimensions_json or "{}"),
        }

    def clear(self) -> None:
        with self.database.session() as session:
            session.query(MetricRow).delete()
