from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_git(root: Path, *arguments: str, binary: bool = False) -> bytes | str:
    completed = subprocess.run(["git", *arguments], cwd=root, capture_output=True, check=True)
    return (
        completed.stdout if binary else completed.stdout.decode("utf-8", errors="replace").strip()
    )


def source_identity(root: Path = ROOT) -> dict[str, object]:
    commit = str(_run_git(root, "rev-parse", "HEAD"))
    dirty = bool(str(_run_git(root, "status", "--porcelain=v1", "--untracked-files=all")))
    raw_paths = _run_git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        binary=True,
    )
    assert isinstance(raw_paths, bytes)
    digest = hashlib.sha256()
    count = 0
    for raw_path in sorted(item for item in raw_paths.split(b"\0") if item):
        relative = raw_path.decode("utf-8")
        path = (root / relative).resolve()
        if not path.is_file() or root.resolve() not in path.parents:
            continue
        payload = path.read_bytes()
        encoded = relative.replace("\\", "/").encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        count += 1
    return {
        "git_commit": commit,
        "git_dirty": dirty,
        "source_tree_sha256": digest.hexdigest(),
        "source_file_count": count,
    }


def artifact_record(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.name,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def build_manifest(artifacts: tuple[Path, ...], root: Path = ROOT) -> dict:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "release": {
            "name": pyproject["project"]["name"],
            "version": pyproject["project"]["version"],
            "channel": "candidate",
            **source_identity(root),
        },
        "artifacts": [artifact_record(path) for path in artifacts],
        "external_acceptance_gates": [
            {"gate": "licensed_speech_corpus_model_selection", "status": "blocked_external"},
            {"gate": "physical_voice_and_devices", "status": "blocked_external"},
            {"gate": "packaged_sleep_restart_notifications", "status": "blocked_external"},
            {"gate": "clean_install_upgrade_uninstall", "status": "blocked_external"},
            {"gate": "authenticode", "status": "blocked_missing_certificate"},
            {"gate": "real_time_24_hour_soak", "status": "blocked_external"},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Hash release artifacts and exact source state.")
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "release-manifest.json")
    parser.add_argument("--checksums", type=Path, default=ROOT / "dist" / "SHA256SUMS")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    artifacts = tuple(path.resolve() for path in args.artifact)
    missing = [str(path) for path in artifacts if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing release artifact(s): " + ", ".join(missing))
    manifest = build_manifest(artifacts)
    if args.require_clean and manifest["release"]["git_dirty"]:
        raise RuntimeError("Release source tree is dirty")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    checksum_lines = [f"{item['sha256']}  {item['path']}" for item in manifest["artifacts"]]
    args.checksums.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    print(
        f"Wrote {args.output} for {len(artifacts)} artifact(s); "
        f"source {manifest['release']['source_tree_sha256']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
