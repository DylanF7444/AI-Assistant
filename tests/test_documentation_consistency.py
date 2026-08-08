from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_documented_schema_versions_match_code() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/check_documentation_consistency.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "capability claims are consistent" in completed.stdout
