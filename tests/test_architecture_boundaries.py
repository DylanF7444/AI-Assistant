from __future__ import annotations

import ast
from pathlib import Path

from assistant.language import planner
from assistant.language.planner_contracts import PlannerDecision

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "assistant"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    values = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.add(node.module)
    return values


def test_core_packages_do_not_depend_on_the_desktop_ui():
    exceptions = {SOURCE_ROOT / "app.py", SOURCE_ROOT / "diagnostics.py"}
    violations = []
    for path in SOURCE_ROOT.rglob("*.py"):
        if path in exceptions or "ui" in path.relative_to(SOURCE_ROOT).parts:
            continue
        for imported in _imports(path):
            if imported == "assistant.ui" or imported.startswith("assistant.ui."):
                violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []


def test_domain_layer_import_boundaries_are_stable():
    forbidden = {
        "policy": ("assistant.ui", "assistant.integrations", "assistant.router"),
        "storage": ("assistant.ui", "assistant.execution", "assistant.router"),
        "integrations": ("assistant.ui", "assistant.execution", "assistant.router"),
        "router": ("assistant.ui", "assistant.integrations", "assistant.execution"),
    }
    violations = []
    for package, prefixes in forbidden.items():
        for path in (SOURCE_ROOT / package).rglob("*.py"):
            for imported in _imports(path):
                if imported.startswith(prefixes):
                    violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []


def test_extracted_planner_contract_keeps_legacy_public_import():
    assert planner.PlannerDecision is PlannerDecision
