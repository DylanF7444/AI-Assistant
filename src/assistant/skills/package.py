from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SIGNATURE_FILE = "signature.json"
MAX_PACKAGE_FILES = 512
MAX_PACKAGE_BYTES = 25 * 1024 * 1024
MAX_SIGNATURE_FILE_BYTES = 4096
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def package_digest(package_dir: Path) -> bytes:
    package_dir = package_dir.resolve()
    digest = hashlib.sha256()
    files = sorted(
        path for path in package_dir.rglob("*") if path.is_file() and path.name != SIGNATURE_FILE
    )
    if not files:
        raise ValueError("Skill package is empty")
    if len(files) > MAX_PACKAGE_FILES:
        raise ValueError(f"Skill package contains more than {MAX_PACKAGE_FILES} files")
    total_bytes = 0
    for path in files:
        if path.is_symlink() or package_dir not in path.resolve().parents:
            raise ValueError("Skill package contains a link or path outside its root")
        relative = path.relative_to(package_dir).as_posix().encode("utf-8")
        if len(relative) > 1000:
            raise ValueError("Skill package contains an overlong path")
        payload = path.read_bytes()
        total_bytes += len(payload)
        if total_bytes > MAX_PACKAGE_BYTES:
            raise ValueError(f"Skill package exceeds {MAX_PACKAGE_BYTES} expanded bytes")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.digest()


def sign_package(package_dir: Path, key_id: str, private_key: Ed25519PrivateKey) -> Path:
    if not _KEY_ID_PATTERN.fullmatch(key_id):
        raise ValueError("Skill signing key ID is invalid")
    signature = private_key.sign(package_digest(package_dir))
    destination = package_dir / SIGNATURE_FILE
    destination.write_text(
        json.dumps(
            {
                "algorithm": "ed25519",
                "key_id": key_id,
                "signature": base64.b64encode(signature).decode("ascii"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return destination


class PackageVerifier:
    def __init__(
        self,
        trusted_keys: dict[str, bytes],
        *,
        revoked_key_ids: set[str] | None = None,
    ) -> None:
        self.trusted_keys = trusted_keys
        self.revoked_key_ids = set(revoked_key_ids or ())

    @staticmethod
    def signature_key_id(package_dir: Path) -> str:
        signature_path = package_dir / SIGNATURE_FILE
        if not signature_path.is_file():
            raise ValueError("Skill package is unsigned")
        if signature_path.is_symlink() or signature_path.stat().st_size > MAX_SIGNATURE_FILE_BYTES:
            raise ValueError("Skill package signature file is unsafe")
        try:
            payload = json.loads(signature_path.read_text("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Skill package signature is invalid JSON") from error
        if not isinstance(payload, dict) or set(payload) != {"algorithm", "key_id", "signature"}:
            raise ValueError("Skill package signature has invalid fields")
        key_id = payload.get("key_id")
        if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
            raise ValueError("Skill package signing key ID is invalid")
        return key_id

    def verify(self, package_dir: Path, *, required: bool = True) -> bool:
        signature_path = package_dir / SIGNATURE_FILE
        if not signature_path.is_file():
            if required:
                raise ValueError("Skill package is unsigned")
            return False
        key_id = self.signature_key_id(package_dir)
        payload = json.loads(signature_path.read_text("utf-8"))
        if payload.get("algorithm") != "ed25519":
            raise ValueError("Unsupported skill signature algorithm")
        if key_id in self.revoked_key_ids:
            raise ValueError("Skill package signer has been revoked")
        if key_id not in self.trusted_keys:
            raise ValueError("Skill package signer is not trusted")
        try:
            signature = base64.b64decode(payload["signature"], validate=True)
            if len(signature) != 64:
                raise ValueError("Ed25519 signatures must contain 64 bytes")
            if len(self.trusted_keys[key_id]) != 32:
                raise ValueError("Ed25519 public keys must contain 32 bytes")
            key = Ed25519PublicKey.from_public_bytes(self.trusted_keys[key_id])
            key.verify(signature, package_digest(package_dir))
        except (InvalidSignature, ValueError, KeyError) as exc:
            raise ValueError("Skill package signature is invalid") from exc
        return True
