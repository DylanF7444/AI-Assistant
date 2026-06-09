from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from assistant.models import ModelManager, ModelManifest


def signed_manifest(source: Path):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    initial = ModelManifest(
        model_id="test-model",
        component="stt",
        filename="model.bin",
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        size_bytes=source.stat().st_size,
        signer_key_id="release",
        signature=base64.b64encode(b"placeholder").decode(),
    )
    signature = private.sign(initial.signed_payload())
    manifest = initial.model_copy(
        update={"signature": base64.b64encode(signature).decode()}
    )
    return manifest, public


def test_offline_model_install_is_verified_and_atomic(tmp_path: Path):
    source = tmp_path / "media" / "model.bin"
    source.parent.mkdir()
    source.write_bytes(b"local model bytes")
    manifest, public = signed_manifest(source)
    manager = ModelManager(tmp_path / "models", {"release": public})
    installed = manager.install_from_file(source, manifest)
    assert installed.read_bytes() == source.read_bytes()
    assert manager.installed()[0]["model_id"] == "test-model"


def test_tampered_model_and_unapproved_download_are_rejected(tmp_path: Path):
    source = tmp_path / "model.bin"
    source.write_bytes(b"original")
    manifest, public = signed_manifest(source)
    source.write_bytes(b"tampered")
    manager = ModelManager(tmp_path / "models", {"release": public})
    with pytest.raises(ValueError, match="size|checksum"):
        manager.install_from_file(source, manifest)
    with pytest.raises(PermissionError):
        manager.download_and_install(manifest)
