from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field


class ModelManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    package_version: str = "1.0"
    model_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    component: str = Field(pattern=r"^(stt|llm|tts|wakeword)$")
    filename: str
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    size_bytes: int = Field(gt=0)
    signer_key_id: str
    signature: str
    source_url: str | None = None

    def signed_payload(self) -> bytes:
        value = self.model_dump(exclude={"signature"})
        return json.dumps(
            value, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


class ModelManager:
    def __init__(
        self, model_dir: Path, trusted_keys: dict[str, bytes] | None = None
    ) -> None:
        self.model_dir = model_dir
        self.trusted_keys = trusted_keys or {}

    def verify_manifest(self, manifest: ModelManifest) -> None:
        key_bytes = self.trusted_keys.get(manifest.signer_key_id)
        if key_bytes is None:
            raise ValueError("Model package signer is not trusted")
        try:
            signature = base64.b64decode(manifest.signature, validate=True)
            Ed25519PublicKey.from_public_bytes(key_bytes).verify(
                signature, manifest.signed_payload()
            )
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("Model manifest signature is invalid") from exc

    @staticmethod
    def verify_file(path: Path, manifest: ModelManifest) -> None:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        if size != manifest.size_bytes:
            raise ValueError("Model package size does not match its manifest")
        if digest.hexdigest().casefold() != manifest.sha256.casefold():
            raise ValueError("Model package checksum is invalid")

    def install_from_file(
        self, source: Path, manifest: ModelManifest
    ) -> Path:
        self.verify_manifest(manifest)
        self.verify_file(source, manifest)
        component_dir = self.model_dir / manifest.component / manifest.model_id
        component_dir.mkdir(parents=True, exist_ok=True)
        destination = component_dir / Path(manifest.filename).name
        with tempfile.NamedTemporaryFile(
            dir=component_dir, delete=False, suffix=".installing"
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            shutil.copyfile(source, temporary_path)
            self.verify_file(temporary_path, manifest)
            temporary_path.replace(destination)
            (component_dir / "manifest.json").write_text(
                manifest.model_dump_json(indent=2), encoding="utf-8"
            )
        finally:
            temporary_path.unlink(missing_ok=True)
        return destination

    def download_and_install(
        self,
        manifest: ModelManifest,
        *,
        allow_network: bool = False,
    ) -> Path:
        if not allow_network:
            raise PermissionError("Model downloads require explicit network approval")
        if not manifest.source_url:
            raise ValueError("Model manifest has no source URL")
        parsed = urllib.parse.urlparse(manifest.source_url)
        if parsed.scheme != "https":
            raise ValueError("Model downloads require HTTPS")
        with tempfile.NamedTemporaryFile(delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with urllib.request.urlopen(
                manifest.source_url, timeout=60
            ) as response, temporary_path.open("wb") as output:
                shutil.copyfileobj(response, output)
            return self.install_from_file(temporary_path, manifest)
        finally:
            temporary_path.unlink(missing_ok=True)

    def installed(self) -> tuple[dict[str, str], ...]:
        values = []
        if not self.model_dir.exists():
            return ()
        for manifest_path in self.model_dir.rglob("manifest.json"):
            try:
                manifest = ModelManifest.model_validate_json(
                    manifest_path.read_text("utf-8")
                )
            except Exception:
                continue
            model_path = manifest_path.parent / Path(manifest.filename).name
            if model_path.is_file():
                values.append(
                    {
                        "model_id": manifest.model_id,
                        "component": manifest.component,
                        "path": str(model_path),
                    }
                )
        return tuple(values)

