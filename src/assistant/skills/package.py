from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SIGNATURE_FILE = "signature.json"


def package_digest(package_dir: Path) -> bytes:
    digest = hashlib.sha256()
    files = sorted(
        path for path in package_dir.rglob("*") if path.is_file() and path.name != SIGNATURE_FILE
    )
    if not files:
        raise ValueError("Skill package is empty")
    for path in files:
        relative = path.relative_to(package_dir).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.digest()


def sign_package(package_dir: Path, key_id: str, private_key: Ed25519PrivateKey) -> Path:
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
    def __init__(self, trusted_keys: dict[str, bytes]) -> None:
        self.trusted_keys = trusted_keys

    def verify(self, package_dir: Path, *, required: bool = True) -> bool:
        signature_path = package_dir / SIGNATURE_FILE
        if not signature_path.is_file():
            if required:
                raise ValueError("Skill package is unsigned")
            return False
        payload = json.loads(signature_path.read_text("utf-8"))
        if payload.get("algorithm") != "ed25519":
            raise ValueError("Unsupported skill signature algorithm")
        key_id = payload.get("key_id")
        if key_id not in self.trusted_keys:
            raise ValueError("Skill package signer is not trusted")
        try:
            signature = base64.b64decode(payload["signature"], validate=True)
            key = Ed25519PublicKey.from_public_bytes(self.trusted_keys[key_id])
            key.verify(signature, package_digest(package_dir))
        except (InvalidSignature, ValueError, KeyError) as exc:
            raise ValueError("Skill package signature is invalid") from exc
        return True
