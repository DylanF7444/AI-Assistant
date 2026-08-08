from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import subprocess
import tomllib
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def _git_value(*arguments: str) -> str | None:
    completed = subprocess.run(
        ["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = completed.stdout.strip()
    return value or None if completed.returncode == 0 else None


def _license(distribution: metadata.Distribution) -> str:
    value = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
    if value and value.strip() and value.strip().casefold() != "unknown":
        return value.strip()[:500]
    classifiers = distribution.metadata.get_all("Classifier") or []
    license_classifiers = [
        item.rsplit("::", 1)[-1].strip() for item in classifiers if "License ::" in item
    ]
    return "; ".join(license_classifiers)[:500] or "NOASSERTION"


def _metadata_hash(distribution: metadata.Distribution) -> str | None:
    payload = distribution.read_text("METADATA")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() if payload else None


def _selected_requirements(pyproject: dict, extras: tuple[str, ...]) -> list[Requirement]:
    values = list(pyproject["project"].get("dependencies", []))
    optional = pyproject["project"].get("optional-dependencies", {})
    for extra in extras:
        if extra not in optional:
            raise ValueError(f"Unknown project extra: {extra}")
        values.extend(optional[extra])
    return [Requirement(value) for value in values]


def build_sbom(extras: tuple[str, ...]) -> dict:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    installed = {
        canonicalize_name(item.metadata["Name"]): item for item in metadata.distributions()
    }
    queue = deque(_selected_requirements(pyproject, extras))
    selected: dict[str, metadata.Distribution] = {}
    dependency_edges: dict[str, set[str]] = {}
    while queue:
        requirement = queue.popleft()
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        distribution = installed.get(name)
        if distribution is None:
            raise RuntimeError(f"Required distribution is not installed: {requirement.name}")
        if name in selected:
            continue
        selected[name] = distribution
        dependencies = set()
        for raw in distribution.requires or ():
            child = Requirement(raw)
            if child.marker and not child.marker.evaluate({"extra": ""}):
                continue
            child_name = canonicalize_name(child.name)
            dependencies.add(child_name)
            queue.append(child)
        dependency_edges[name] = dependencies

    project_name = canonicalize_name(pyproject["project"]["name"])
    version = pyproject["project"]["version"]
    root_ref = f"pkg:pypi/{project_name}@{version}"
    components = []
    references = {name: f"pkg:pypi/{name}@{dist.version}" for name, dist in selected.items()}
    for name, distribution in sorted(selected.items()):
        component = {
            "type": "library",
            "bom-ref": references[name],
            "name": distribution.metadata["Name"],
            "version": distribution.version,
            "purl": references[name],
            "licenses": [{"license": {"name": _license(distribution)}}],
            "properties": [{"name": "assistant:installed-metadata", "value": "true"}],
        }
        digest = _metadata_hash(distribution)
        if digest:
            component["hashes"] = [{"alg": "SHA-256", "content": digest}]
        components.append(component)
    dependencies = [
        {"ref": root_ref, "dependsOn": sorted(references.values())},
        *(
            {
                "ref": references[name],
                "dependsOn": sorted(references[child] for child in children if child in references),
            }
            for name, children in sorted(dependency_edges.items())
        ),
    ]
    commit = _git_value("rev-parse", "HEAD") or "unknown"
    serial_seed = f"{project_name}:{version}:{commit}:{','.join(sorted(extras))}"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid5(NAMESPACE_URL, serial_seed)}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "tools": [{"vendor": "Local Assistant", "name": "generate_sbom.py"}],
            "component": {
                "type": "application",
                "bom-ref": root_ref,
                "name": pyproject["project"]["name"],
                "version": version,
                "purl": root_ref,
            },
            "properties": [
                {"name": "assistant:git-commit", "value": commit},
                {"name": "assistant:selected-extras", "value": ",".join(sorted(extras))},
            ],
        },
        "components": components,
        "dependencies": dependencies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a local CycloneDX release SBOM.")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "sbom.cdx.json")
    parser.add_argument(
        "--extras",
        default="desktop,audio,stt,wakeword,integrations,browser,documents,vision",
        help="Comma-separated installed project extras represented in the release.",
    )
    args = parser.parse_args()
    extras = tuple(item.strip() for item in args.extras.split(",") if item.strip())
    payload = build_sbom(extras)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} with {len(payload['components'])} dependency components.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
