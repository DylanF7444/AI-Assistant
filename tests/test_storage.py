from pathlib import Path

from sqlalchemy import inspect, text

from assistant.storage.database import (
    AuditStore,
    CommandCenterArtifactRevisionRow,
    CommandCenterArtifactRow,
    CommandCenterDeliveryRow,
    CommandCenterObservationCursorRow,
    DashboardMetricSampleRow,
    Database,
    KnowledgeBaseRow,
    MacroRow,
    MemoryItemRow,
    NotificationRow,
    SchemaVersionRow,
    SettingsStore,
    VectorItemRow,
)
from assistant.storage.knowledge import KnowledgeBaseStore


def test_database_migration_integrity_and_backup(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    database.migrate()
    assert database.integrity_check()
    backup = tmp_path / "backup.db"
    database.backup(backup)
    assert backup.exists()


def test_settings_and_audit_redaction(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    settings = SettingsStore(database)
    settings.set("theme", {"name": "dark"})
    assert settings.get("theme") == {"name": "dark"}
    audit = AuditStore(database)
    audit.record(
        correlation_id="c",
        event_type="test",
        details={
            "token": "private",
            "nested": {"password": "private"},
            "url": "https://user:secret@example.com/photo.png?signature=private",
        },
    )
    row = audit.query()[0]
    assert row["details"]["token"] == "[REDACTED]"
    assert row["details"]["nested"]["password"] == "[REDACTED]"
    assert row["details"]["url"] == "https://example.com/photo.png"


def test_upgrade_from_schema_version_one(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    with database.session() as session:
        session.get(SchemaVersionRow, 1).version = 1
    MacroRow.__table__.drop(database.engine)
    database.migrate()
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == Database.CURRENT_SCHEMA_VERSION
    assert inspect(database.engine).has_table("macros")


def test_corrupt_database_can_restore_verified_backup(tmp_path: Path):
    path = tmp_path / "data.db"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    SettingsStore(database).set("value", "preserved")
    backup = tmp_path / "backup.db"
    database.backup(backup)
    database.engine.dispose()
    path.write_bytes(b"not a sqlite database")
    broken = Database(f"sqlite:///{path}")
    assert not broken.integrity_check()
    assert broken.recover(backup)
    assert SettingsStore(broken).get("value") == "preserved"


def test_knowledge_base_crud_import_and_retrieval(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    store = KnowledgeBaseStore(database)
    saved = store.save(
        "Personal preferences",
        "Dylan prefers concise answers and uses the America/Chicago time zone.",
        description="Response and scheduling preferences",
    )
    source = tmp_path / "project.md"
    source.write_text(
        "# Atlas\n\nThe Atlas project launch date is October 14.",
        encoding="utf-8",
    )
    imported = store.import_file(source)

    assert store.get(saved.id).enabled
    assert imported.source_path == str(source.resolve())
    excerpts = store.search("When does the Atlas project launch?")
    assert excerpts[0].name == "project"
    assert "October 14" in excerpts[0].text

    store.set_enabled(imported.id, False)
    assert store.search("When does the Atlas project launch?") == ()
    store.delete(saved.id)
    assert [item.id for item in store.list()] == [imported.id]


def test_upgrade_from_schema_version_three_adds_knowledge_bases(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    with database.session() as session:
        session.get(SchemaVersionRow, 1).version = 3
    KnowledgeBaseRow.__table__.drop(database.engine)

    database.migrate()

    assert inspect(database.engine).has_table("knowledge_bases")
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == Database.CURRENT_SCHEMA_VERSION


def test_upgrade_from_schema_version_four_adds_vector_items(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    with database.session() as session:
        session.get(SchemaVersionRow, 1).version = 4
    VectorItemRow.__table__.drop(database.engine)

    database.migrate()

    assert inspect(database.engine).has_table("vector_items")
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == Database.CURRENT_SCHEMA_VERSION


def test_upgrade_from_schema_version_five_adds_memory_items(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    with database.session() as session:
        session.get(SchemaVersionRow, 1).version = 5
    MemoryItemRow.__table__.drop(database.engine)

    database.migrate()

    assert inspect(database.engine).has_table("memory_items")
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == Database.CURRENT_SCHEMA_VERSION


def test_upgrade_from_schema_version_eight_adds_dashboard_metric_samples(
    tmp_path: Path,
):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    with database.session() as session:
        session.get(SchemaVersionRow, 1).version = 8
    DashboardMetricSampleRow.__table__.drop(database.engine)

    database.migrate()

    assert inspect(database.engine).has_table("dashboard_metric_samples")
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == Database.CURRENT_SCHEMA_VERSION


def test_upgrade_from_schema_version_nine_adds_notification_inbox(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    with database.session() as session:
        session.get(SchemaVersionRow, 1).version = 9
    NotificationRow.__table__.drop(database.engine)

    database.migrate()

    assert inspect(database.engine).has_table("notifications")
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == Database.CURRENT_SCHEMA_VERSION


def test_upgrade_from_schema_version_eleven_adds_metric_dimensions(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    with database.session() as session:
        session.execute(text("ALTER TABLE pipeline_metrics DROP COLUMN dimensions_json"))
        session.get(SchemaVersionRow, 1).version = 11

    database.migrate()

    columns = {
        column["name"] for column in inspect(database.engine).get_columns("pipeline_metrics")
    }
    assert "dimensions_json" in columns
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == Database.CURRENT_SCHEMA_VERSION


def test_upgrade_from_schema_version_twelve_adds_command_center_artifacts(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    for row in (
        CommandCenterDeliveryRow,
        CommandCenterArtifactRevisionRow,
        CommandCenterArtifactRow,
    ):
        row.__table__.drop(database.engine)
    with database.session() as session:
        session.get(SchemaVersionRow, 1).version = 12

    database.migrate()

    inspector = inspect(database.engine)
    assert inspector.has_table("command_center_artifacts")
    assert inspector.has_table("command_center_artifact_revisions")
    assert inspector.has_table("command_center_deliveries")
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == Database.CURRENT_SCHEMA_VERSION


def test_upgrade_from_schema_version_thirteen_adds_artifact_run_origin(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_command_center_artifacts_origin_run_id"))
        connection.execute(
            text(
                "CREATE TABLE command_center_artifacts_legacy AS "
                "SELECT id, current_revision, sensitive, expires_at, created_at, updated_at "
                "FROM command_center_artifacts"
            )
        )
        connection.execute(text("DROP TABLE command_center_artifacts"))
        connection.execute(
            text("ALTER TABLE command_center_artifacts_legacy RENAME TO command_center_artifacts")
        )
    with database.session() as session:
        session.get(SchemaVersionRow, 1).version = 13

    database.migrate()

    columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("command_center_artifacts")
    }
    assert "origin_run_id" in columns
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == Database.CURRENT_SCHEMA_VERSION


def test_upgrade_from_schema_version_fourteen_adds_observation_cursors(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    CommandCenterObservationCursorRow.__table__.drop(database.engine)
    with database.session() as session:
        session.get(SchemaVersionRow, 1).version = 14

    database.migrate()

    assert inspect(database.engine).has_table("command_center_observation_cursors")
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == Database.CURRENT_SCHEMA_VERSION
