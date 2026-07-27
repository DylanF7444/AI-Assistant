from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from assistant.config import Settings
from assistant.extensions.platform import (
    CatalogExtension,
    ExtensionCatalog,
    ExtensionHubService,
    ExtensionPlatformClient,
)
from assistant.skills.package import sign_package
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import AuditStore, Database

SKILL_SOURCE = """
import asyncio
from pydantic import BaseModel
from assistant.contracts import PermissionScope, ResultStatus, SkillCall, SkillManifest, SkillResult
from assistant.skills.base import Skill

class Input(BaseModel):
    text: str

class Output(BaseModel):
    echoed: str

class EchoSkill(Skill):
    input_model = Input
    output_model = Output
    manifest = SkillManifest(
        skill_id="community.echo",
        version="1.2.0",
        display_name="Community Echo",
        category="Productivity",
        description="Echo a supplied value from a catalog-installed skill.",
        input_schema=Input.model_json_schema(),
        output_schema=Output.model_json_schema(),
        permissions=(PermissionScope(capability="network", reason="Reach an approved service."),),
        entry_point="plugin:EchoSkill",
    )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        arguments = self.validate(call.arguments)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Echoed.",
            data={"echoed": arguments.text},
        )
"""


def _signed_skill_archive(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "plugin.py").write_text(SKILL_SOURCE, encoding="utf-8")
    namespace: dict = {}
    exec(SKILL_SOURCE, namespace)
    manifest = namespace["EchoSkill"].manifest
    (package / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    sign_package(package, "community-test", private)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.iterdir()):
            archive.write(path, path.name)
    return output.getvalue(), manifest, public


def _catalog_entry(archive: bytes, manifest, **updates) -> CatalogExtension:
    values = {
        "extension_id": manifest.skill_id,
        "extension_type": "skill",
        "version": manifest.version,
        "api_version": manifest.api_version,
        "display_name": manifest.display_name,
        "description": manifest.description,
        "publisher": "Community Test Publisher",
        "category": manifest.category,
        "package_url": "https://extensions.example.test/community.echo.zip",
        "package_sha256": hashlib.sha256(archive).hexdigest(),
        "permissions": manifest.permissions,
        "risk": manifest.risk,
    }
    values.update(updates)
    return CatalogExtension.model_validate(values)


def _service(tmp_path: Path, entry: CatalogExtension, archive: bytes, public: bytes):
    catalog_url = "https://extensions.example.test/v1/catalog.json"
    catalog = ExtensionCatalog(extensions=(entry,)).model_dump_json().encode()
    responses = {catalog_url: catalog, entry.package_url: archive}

    def read(url: str, max_bytes: int, timeout_seconds: float) -> bytes:
        assert timeout_seconds == 4
        payload = responses[url]
        assert len(payload) <= max_bytes
        return payload

    registry = SkillRegistry(
        tmp_path / "installed",
        trusted_signing_keys={"community-test": public},
    )
    database = Database(f"sqlite:///{(tmp_path / 'audit.db').as_posix()}")
    database.migrate()
    audit = AuditStore(database)
    client = ExtensionPlatformClient(catalog_url, timeout_seconds=4, reader=read)
    return ExtensionHubService(registry, client, audit=audit), registry, audit


@pytest.mark.asyncio
async def test_extension_hub_browses_and_installs_a_verified_skill_disabled(tmp_path: Path):
    archive, manifest, public = _signed_skill_archive(tmp_path)
    entry = _catalog_entry(archive, manifest)
    service, registry, audit = _service(tmp_path, entry, archive, public)

    entries = await service.refresh()
    installed = await service.install(entry.extension_id)

    assert entries == (entry,)
    assert installed == manifest
    assert service.installation_status(entry.extension_id) == "installed"
    assert registry.inspect(entry.extension_id).manifest == manifest
    assert registry.is_enabled(entry.extension_id) is False
    event = audit.query(event_type="extension_installation")[0]
    assert event["decision"] == "installed_disabled"
    assert event["permissions"] == ["network"]

    restarted = SkillRegistry(
        registry.install_dir,
        trusted_signing_keys={"community-test": public},
    )
    assert restarted.load_all_installed() == {}
    assert restarted.inspect(entry.extension_id).manifest == manifest
    assert restarted.is_enabled(entry.extension_id) is False


@pytest.mark.asyncio
async def test_extension_hub_rejects_a_download_with_the_wrong_digest(tmp_path: Path):
    archive, manifest, public = _signed_skill_archive(tmp_path)
    entry = _catalog_entry(archive, manifest, package_sha256="0" * 64)
    service, registry, audit = _service(tmp_path, entry, archive, public)
    await service.refresh()

    with pytest.raises(ValueError, match="digest"):
        await service.install(entry.extension_id)

    assert not service.is_installed(entry.extension_id)
    assert audit.query(event_type="extension_installation")[0]["decision"] == "failed"


@pytest.mark.asyncio
async def test_extension_hub_rejects_archive_path_traversal(tmp_path: Path):
    _archive, manifest, public = _signed_skill_archive(tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../escape.py", "bad")
        archive.writestr("manifest.json", manifest.model_dump_json())
    malicious = output.getvalue()
    entry = _catalog_entry(malicious, manifest)
    service, _registry, _audit = _service(tmp_path, entry, malicious, public)
    await service.refresh()

    with pytest.raises(ValueError, match="unsafe path"):
        await service.install(entry.extension_id)

    assert not (tmp_path / "escape.py").exists()


def test_extension_catalog_settings_require_https_without_credentials(tmp_path: Path):
    assert (
        Settings(
            data_dir=tmp_path,
            extension_catalog_url="https://extensions.example.test/catalog.json",
        ).extension_catalog_url
        == "https://extensions.example.test/catalog.json"
    )
    assert Settings(data_dir=tmp_path, extension_catalog_url="  ").extension_catalog_url is None
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(data_dir=tmp_path, extension_catalog_url="http://localhost/catalog.json")
    with pytest.raises(ValueError, match="credentials"):
        Settings(
            data_dir=tmp_path,
            extension_catalog_url="https://user:secret@example.test/catalog.json",
        )


def test_trusted_skill_key_can_be_represented_in_settings(tmp_path: Path):
    encoded = base64.b64encode(b"x" * 32).decode("ascii")
    settings = Settings(data_dir=tmp_path, trusted_skill_keys={"publisher": encoded})
    assert settings.trusted_skill_keys == {"publisher": encoded}
