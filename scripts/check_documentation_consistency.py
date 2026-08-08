from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASE_SOURCE = ROOT / "src" / "assistant" / "storage" / "database.py"
VERSION_REFERENCES = {
    ROOT / "IMPLEMENTATION_STATUS.md": re.compile(r"schema version (?P<version>\d+) migrations"),
    ROOT / "docs" / "COMPATIBILITY.md": re.compile(
        r"Database \| SQLite schema version (?P<version>\d+)"
    ),
}
FEATURE_CATALOG = ROOT / "docs" / "FEATURES.md"
REQUIRED_SKILL_IDS = (
    "documents.read",
    "documents.content_search",
    "vision.understand_image",
    "integration.google_drive",
    "integration.outlook_mail",
    "integration.outlook_calendar",
    "integration.onedrive",
    "integration.slack",
    "integration.notion",
    "integration.github",
)
REQUIRED_ROADMAP_DOCUMENTS = (
    ROOT / "docs" / "ROADMAP_EXECUTION_REPORT.md",
    ROOT / "docs" / "CAPABILITY_ACCEPTANCE_AND_THREAT_REVIEW.md",
    ROOT / "docs" / "RELEASE_AND_SIGNING_RUNBOOK.md",
)
FORBIDDEN_CURRENT_CLAIMS = {
    ROOT / "docs" / "FEATURES.md": (
        "Content downloads and Drive changes are deferred",
        "Sending, drafting, moving, and deleting are deferred",
        "Event changes and responses are deferred",
        "Content transfer and changes are deferred",
        "Sends and file actions are deferred",
        "Writes and broad ingestion are deferred",
        "Writes and secrets access are deferred",
    ),
    ROOT / "docs" / "USER_GUIDE.md": (
        "Content downloads, uploads, moves, sharing changes, and deletes are intentionally not supported",
        "shared by the three read-only extensions",
        "Sending or editing mail, changing calendar events",
    ),
}


def current_schema_version() -> int:
    tree = ast.parse(DATABASE_SOURCE.read_text(encoding="utf-8"), filename=str(DATABASE_SOURCE))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Database":
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "CURRENT_SCHEMA_VERSION"
                for target in statement.targets
            ):
                continue
            value = ast.literal_eval(statement.value)
            if not isinstance(value, int):
                raise ValueError("Database.CURRENT_SCHEMA_VERSION must be an integer")
            return value
    raise ValueError("Database.CURRENT_SCHEMA_VERSION was not found")


def documentation_errors() -> tuple[str, ...]:
    expected = current_schema_version()
    errors: list[str] = []
    for path, pattern in VERSION_REFERENCES.items():
        text = path.read_text(encoding="utf-8")
        match = pattern.search(text)
        relative = path.relative_to(ROOT)
        if match is None:
            errors.append(f"{relative}: documented schema version was not found")
            continue
        documented = int(match.group("version"))
        if documented != expected:
            errors.append(
                f"{relative}: documents schema {documented}, but code uses schema {expected}"
            )
    catalog = FEATURE_CATALOG.read_text(encoding="utf-8")
    for skill_id in REQUIRED_SKILL_IDS:
        if f"`{skill_id}`" not in catalog:
            errors.append(f"docs/FEATURES.md: missing current skill ID {skill_id}")
    for path in REQUIRED_ROADMAP_DOCUMENTS:
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            errors.append(f"{path.relative_to(ROOT)}: required roadmap evidence is missing")
    for path, forbidden_claims in FORBIDDEN_CURRENT_CLAIMS.items():
        text = path.read_text(encoding="utf-8")
        for claim in forbidden_claims:
            if claim in text:
                errors.append(f"{path.relative_to(ROOT)}: stale claim remains: {claim}")
    return tuple(errors)


def main() -> int:
    errors = documentation_errors()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(
        "Documentation schema, feature catalog, roadmap evidence, and current "
        f"capability claims are consistent (schema {current_schema_version()})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
