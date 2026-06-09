from pathlib import Path

from sqlalchemy import inspect

from assistant.storage.database import AuditStore, Database, SettingsStore
from assistant.storage.database import MacroRow, SchemaVersionRow


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
        details={"token": "private", "nested": {"password": "private"}},
    )
    row = audit.query()[0]
    assert row["details"]["token"] == "[REDACTED]"
    assert row["details"]["nested"]["password"] == "[REDACTED]"


def test_upgrade_from_schema_version_one(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'old.db'}")
    database.migrate()
    with database.session() as session:
        session.get(SchemaVersionRow, 1).version = 1
    MacroRow.__table__.drop(database.engine)
    database.migrate()
    with database.session() as session:
        assert session.get(SchemaVersionRow, 1).version == 3
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
