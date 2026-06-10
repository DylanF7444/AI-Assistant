from __future__ import annotations

from pathlib import Path

import pytest

from assistant.storage.database import AuditStore, Database


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """Create a fresh SQLite database for every test."""
    value = Database(f"sqlite:///{tmp_path / 'learning.db'}")
    value.migrate()
    return value


@pytest.fixture
def audit(database: Database) -> AuditStore:
    return AuditStore(database)
