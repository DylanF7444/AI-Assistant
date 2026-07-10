from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field


class UpdateFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    size_bytes: int = Field(ge=0)


class UpdateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: str = "1.0"
    application_version: str
    signer_key_id: str
    files: tuple[UpdateFile, ...]
    signature: str

    def signed_payload(self) -> bytes:
        return json.dumps(
            self.model_dump(exclude={"signature"}),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


def _safe_relative(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Unsafe update path: {value}")
    return Path(*path.parts)


class UpdateManager:
    def __init__(
        self,
        staging_root: Path,
        trusted_keys: dict[str, bytes],
        *,
        current_version: str,
    ) -> None:
        self.staging_root = staging_root
        self.trusted_keys = trusted_keys
        self.current_version = Version(current_version)

    def verify_manifest(self, manifest: UpdateManifest) -> None:
        if Version(manifest.application_version) <= self.current_version:
            raise ValueError("Update version must be newer than the current version")
        key = self.trusted_keys.get(manifest.signer_key_id)
        if key is None:
            raise ValueError("Update signer is not trusted")
        try:
            signature = base64.b64decode(manifest.signature, validate=True)
            Ed25519PublicKey.from_public_bytes(key).verify(signature, manifest.signed_payload())
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("Update signature is invalid") from exc

    def stage(self, package: Path) -> Path:
        with zipfile.ZipFile(package) as archive:
            if "update-manifest.json" not in archive.namelist():
                raise ValueError("Update package has no manifest")
            manifest = UpdateManifest.model_validate_json(archive.read("update-manifest.json"))
            self.verify_manifest(manifest)
            expected = {item.path: item for item in manifest.files}
            for name in archive.namelist():
                if name == "update-manifest.json" or name.endswith("/"):
                    continue
                _safe_relative(name)
                if name not in expected:
                    raise ValueError(f"Unsigned file in update package: {name}")
            destination = (self.staging_root / manifest.application_version).resolve()
            root = self.staging_root.resolve()
            if root not in destination.parents:
                raise ValueError("Unsafe update destination")
            temporary = Path(tempfile.mkdtemp(prefix="update-", dir=root.parent))
            try:
                for name, item in expected.items():
                    relative = _safe_relative(name)
                    payload = archive.read(name)
                    if len(payload) != item.size_bytes:
                        raise ValueError(f"Update size mismatch: {name}")
                    if hashlib.sha256(payload).hexdigest() != item.sha256:
                        raise ValueError(f"Update checksum mismatch: {name}")
                    target = temporary / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                (temporary / "update-manifest.json").write_bytes(
                    archive.read("update-manifest.json")
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.rmtree(destination, ignore_errors=True)
                temporary.replace(destination)
                return destination
            finally:
                shutil.rmtree(temporary, ignore_errors=True)

    def apply(self, staged: Path, install_dir: Path) -> Path:
        manifest = UpdateManifest.model_validate_json(
            (staged / "update-manifest.json").read_text("utf-8")
        )
        self.verify_manifest(manifest)
        backup = install_dir / ".update-backup"
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True)
        for item in manifest.files:
            relative = _safe_relative(item.path)
            source = staged / relative
            destination = (install_dir / relative).resolve()
            if install_dir.resolve() not in destination.parents:
                raise ValueError("Unsafe install destination")
            if destination.exists():
                old = backup / relative
                old.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, old)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        return backup
