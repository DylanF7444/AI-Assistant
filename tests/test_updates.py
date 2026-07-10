from __future__ import annotations

import base64
import hashlib
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from assistant.updates import UpdateFile, UpdateManager, UpdateManifest


def make_update(tmp_path: Path, *, path="LocalAssistant.exe", content=b"new"):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    initial = UpdateManifest(
        application_version="1.1.0",
        signer_key_id="release",
        files=(
            UpdateFile(
                path=path,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            ),
        ),
        signature=base64.b64encode(b"placeholder").decode(),
    )
    manifest = initial.model_copy(
        update={"signature": base64.b64encode(private.sign(initial.signed_payload())).decode()}
    )
    package = tmp_path / "update.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("update-manifest.json", manifest.model_dump_json())
        archive.writestr(path, content)
    return package, public


def test_signed_update_stages_and_applies_with_backup(tmp_path):
    package, public = make_update(tmp_path)
    manager = UpdateManager(tmp_path / "staging", {"release": public}, current_version="1.0.0")
    staged = manager.stage(package)
    install = tmp_path / "install"
    install.mkdir()
    executable = install / "LocalAssistant.exe"
    executable.write_bytes(b"old")
    backup = manager.apply(staged, install)
    assert executable.read_bytes() == b"new"
    assert (backup / "LocalAssistant.exe").read_bytes() == b"old"


def test_update_rejects_traversal_and_tampering(tmp_path):
    package, public = make_update(tmp_path, path="../escape.exe")
    manager = UpdateManager(tmp_path / "staging", {"release": public}, current_version="1.0.0")
    with pytest.raises(ValueError, match="Unsafe"):
        manager.stage(package)

    package, public = make_update(tmp_path, content=b"expected")
    with zipfile.ZipFile(package) as archive:
        manifest_bytes = archive.read("update-manifest.json")
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("update-manifest.json", manifest_bytes)
        archive.writestr("LocalAssistant.exe", b"tampered")
    manager = UpdateManager(tmp_path / "staging2", {"release": public}, current_version="1.0.0")
    with pytest.raises(ValueError, match="checksum|size"):
        manager.stage(package)
