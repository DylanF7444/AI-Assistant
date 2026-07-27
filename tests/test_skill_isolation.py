from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from assistant.contracts import PermissionContext, PermissionScope, ResultStatus, SkillCall
from assistant.skills.package import sign_package
from assistant.skills.registry import SkillRegistry

SKILL_SOURCE = """
import asyncio
from pydantic import BaseModel
from assistant.contracts import ResultStatus, SkillCall, SkillManifest, SkillResult
from assistant.skills.base import Skill

class Input(BaseModel):
    text: str
    delay: float = 0
    network_test: bool = False
    environment_name: str | None = None

class Output(BaseModel):
    echoed: str

class EchoSkill(Skill):
    input_model = Input
    output_model = Output
    manifest = SkillManifest(
        skill_id="test.isolated",
        version="1.0.0",
        display_name="Isolated Echo",
        description="Test isolated execution",
        input_schema=Input.model_json_schema(),
        output_schema=Output.model_json_schema(),
        entry_point="plugin:EchoSkill",
    )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        await asyncio.sleep(args.delay)
        echoed = args.text
        if args.environment_name:
            import os
            echoed = os.environ.get(args.environment_name, "missing")
        if args.network_test:
            import socket
            try:
                socket.create_connection(("example.com", 80), timeout=0.1)
                echoed = "connected"
            except PermissionError:
                echoed = "blocked"
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Echoed.",
            data={"echoed": echoed},
        )
"""


def make_package(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "plugin.py").write_text(SKILL_SOURCE, encoding="utf-8")
    namespace = {}
    exec(SKILL_SOURCE, namespace)
    manifest = namespace["EchoSkill"].manifest
    (package / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    sign_package(package, "test-key", private)
    return package, public


def test_signed_package_verification_and_tamper_detection(tmp_path):
    package, public = make_package(tmp_path)
    registry = SkillRegistry(tmp_path / "installed", trusted_signing_keys={"test-key": public})
    assert registry.validate_package(package).skill_id == "test.isolated"
    (package / "plugin.py").write_text(SKILL_SOURCE + "\n# changed", encoding="utf-8")
    with pytest.raises(ValueError, match="signature"):
        registry.validate_package(package)


def test_unsigned_package_is_rejected(tmp_path):
    package, public = make_package(tmp_path)
    (package / "signature.json").unlink()
    registry = SkillRegistry(tmp_path / "installed", trusted_signing_keys={"test-key": public})
    with pytest.raises(ValueError, match="unsigned"):
        registry.validate_package(package)


@pytest.mark.asyncio
async def test_installed_skill_executes_in_isolated_worker(tmp_path):
    package, public = make_package(tmp_path)
    registry = SkillRegistry(tmp_path / "installed", trusted_signing_keys={"test-key": public})
    registry.install(package)
    skill = registry.load_installed("test.isolated", timeout_seconds=5)
    call = SkillCall(
        skill_id="test.isolated",
        arguments={"text": "hello"},
        permission_context=PermissionContext(session_id="test"),
    )
    result = await skill.execute(call, __import__("asyncio").Event())
    assert result.status == ResultStatus.COMPLETED
    assert result.data == {"echoed": "hello"}


@pytest.mark.asyncio
async def test_isolated_skill_timeout_terminates_worker(tmp_path):
    package, public = make_package(tmp_path)
    registry = SkillRegistry(tmp_path / "installed", trusted_signing_keys={"test-key": public})
    registry.install(package)
    skill = registry.load_installed("test.isolated", timeout_seconds=0.1)
    call = SkillCall(
        skill_id="test.isolated",
        arguments={"text": "late", "delay": 5},
        permission_context=PermissionContext(session_id="test"),
    )
    result = await skill.execute(call, __import__("asyncio").Event())
    assert result.status == ResultStatus.TIMED_OUT
    assert skill._process is None


@pytest.mark.asyncio
async def test_isolated_skill_worker_blocks_outbound_network(tmp_path):
    package, public = make_package(tmp_path)
    registry = SkillRegistry(tmp_path / "installed", trusted_signing_keys={"test-key": public})
    registry.install(package)
    skill = registry.load_installed("test.isolated", timeout_seconds=5)
    call = SkillCall(
        skill_id="test.isolated",
        arguments={"text": "network", "network_test": True},
        permission_context=PermissionContext(session_id="test"),
    )
    result = await skill.execute(call, __import__("asyncio").Event())
    assert result.status == ResultStatus.COMPLETED
    assert result.data == {"echoed": "blocked"}


@pytest.mark.asyncio
async def test_isolated_skill_worker_does_not_inherit_parent_secrets(tmp_path, monkeypatch):
    package, public = make_package(tmp_path)
    registry = SkillRegistry(tmp_path / "installed", trusted_signing_keys={"test-key": public})
    registry.install(package)
    skill = registry.load_installed("test.isolated", timeout_seconds=5)
    monkeypatch.setenv("ASSISTANT_TEST_SECRET", "must-not-cross-worker-boundary")
    call = SkillCall(
        skill_id="test.isolated",
        arguments={"text": "environment", "environment_name": "ASSISTANT_TEST_SECRET"},
        permission_context=PermissionContext(session_id="test"),
    )

    result = await skill.execute(call, __import__("asyncio").Event())

    assert result.status == ResultStatus.COMPLETED
    assert result.data == {"echoed": "missing"}


def test_skill_update_requires_review_for_added_capabilities(tmp_path):
    package, public = make_package(tmp_path)
    registry = SkillRegistry(tmp_path / "installed", trusted_signing_keys={"test-key": public})
    registry.install(package)
    update = tmp_path / "update"
    __import__("shutil").copytree(package, update)
    manifest = __import__(
        "assistant.contracts", fromlist=["SkillManifest"]
    ).SkillManifest.model_validate_json((update / "manifest.json").read_text("utf-8"))
    manifest = manifest.model_copy(
        update={
            "version": "1.1.0",
            "permissions": (
                PermissionScope(
                    capability="filesystem.read",
                    reason="Read approved files.",
                ),
            ),
        }
    )
    (update / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    (update / "signature.json").unlink()
    # Reuse the original helper's key material by creating a newly trusted key.
    private = Ed25519PrivateKey.generate()
    update_public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    registry.verifier.trusted_keys["update-key"] = update_public
    sign_package(update, "update-key", private)
    with pytest.raises(PermissionError, match="filesystem.read"):
        registry.update(update)
    updated, added = registry.update(update, permission_reviewed=True)
    assert updated.version == "1.1.0"
    assert added == ("filesystem.read",)


def test_frozen_build_relaunches_the_application_in_worker_mode(tmp_path, monkeypatch):
    package, public = make_package(tmp_path)
    registry = SkillRegistry(tmp_path / "installed", trusted_signing_keys={"test-key": public})
    registry.install(package)
    skill = registry.load_installed("test.isolated")
    monkeypatch.setattr("assistant.skills.subprocess.sys.frozen", True, raising=False)

    command = skill._worker_command()

    assert command[:2] == (__import__("sys").executable, "--isolated-skill-worker")
    assert command[-2:] == (str(skill.package_dir), skill.manifest.entry_point)
