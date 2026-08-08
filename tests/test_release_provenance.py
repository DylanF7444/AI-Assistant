from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.generate_release_manifest import artifact_record, build_manifest, source_identity
from scripts.generate_sbom import build_sbom

ROOT = Path(__file__).resolve().parents[1]


def test_release_artifact_record_contains_size_and_sha256(tmp_path):
    artifact = tmp_path / "candidate.exe"
    artifact.write_bytes(b"candidate")

    record = artifact_record(artifact)

    assert record == {
        "path": "candidate.exe",
        "size_bytes": 9,
        "sha256": hashlib.sha256(b"candidate").hexdigest(),
    }


def test_release_manifest_keeps_external_gates_blocked(tmp_path):
    artifact = tmp_path / "candidate.exe"
    artifact.write_bytes(b"candidate")

    manifest = build_manifest((artifact,))
    gates = {item["gate"]: item["status"] for item in manifest["external_acceptance_gates"]}

    assert gates["licensed_speech_corpus_model_selection"] == "blocked_external"
    assert gates["clean_install_upgrade_uninstall"] == "blocked_external"
    assert gates["authenticode"] == "blocked_missing_certificate"


def test_source_identity_is_exact_and_reports_current_worktree():
    identity = source_identity()

    assert len(identity["git_commit"]) == 40
    assert len(identity["source_tree_sha256"]) == 64
    assert identity["source_file_count"] > 300
    assert identity["git_dirty"] is True


def test_sbom_has_root_component_dependencies_and_licenses():
    payload = build_sbom(("test",))

    assert payload["bomFormat"] == "CycloneDX"
    assert payload["metadata"]["component"]["name"] == "local-assistant"
    assert payload["components"]
    assert all(component["licenses"] for component in payload["components"])


def test_packaging_spec_excludes_ambient_optional_ml_and_download_stacks():
    spec = (ROOT / "packaging" / "local-assistant.spec").read_text("utf-8")

    for package in ("torch", "transformers", "yt_dlp", "pandas", "langchain"):
        assert f'"{package}"' in spec
