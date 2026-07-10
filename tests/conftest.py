from __future__ import annotations

from pathlib import Path

import pytest

from assistant.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(profile="testing", data_dir=tmp_path, retain_transcripts_days=0)
