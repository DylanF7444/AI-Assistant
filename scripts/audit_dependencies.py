from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXTRAS = (
    "desktop",
    "audio",
    "stt",
    "wakeword",
    "integrations",
    "browser",
    "documents",
    "vision",
)


def selected_requirements(extras: tuple[str, ...]) -> tuple[str, ...]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    requirements = list(pyproject["project"].get("dependencies", ()))
    optional = pyproject["project"].get("optional-dependencies", {})
    for extra in extras:
        if extra not in optional:
            raise ValueError(f"Unknown project extra: {extra}")
        requirements.extend(optional[extra])
    return tuple(dict.fromkeys(requirements))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit the isolated production dependency graph with pip-audit."
    )
    parser.add_argument(
        "--extras",
        default=",".join(DEFAULT_EXTRAS),
        help="Comma-separated production extras to resolve and audit.",
    )
    args = parser.parse_args()
    extras = tuple(item.strip() for item in args.extras.split(",") if item.strip())
    requirements = selected_requirements(extras)
    with tempfile.TemporaryDirectory(prefix="assistant-audit-") as temp_directory:
        requirements_path = Path(temp_directory) / "requirements.txt"
        requirements_path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--strict",
                "--requirement",
                str(requirements_path),
            ],
            cwd=ROOT,
            check=False,
        )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
