from __future__ import annotations

import importlib
import tempfile
import shutil
from pathlib import Path
from typing import Any
from packaging.version import Version

from assistant import API_VERSION
from assistant.contracts import SkillManifest
from assistant.skills.base import Skill
from assistant.skills.package import PackageVerifier
from assistant.skills.subprocess import IsolatedSkill


class SkillRegistry:
    def __init__(
        self,
        install_dir: Path | None = None,
        *,
        trusted_signing_keys: dict[str, bytes] | None = None,
        require_signatures: bool = True,
    ) -> None:
        self._skills: dict[str, Skill] = {}
        self._enabled: set[str] = set()
        self.install_dir = install_dir
        self.verifier = PackageVerifier(trusted_signing_keys or {})
        self.require_signatures = require_signatures

    def register(self, skill: Skill, *, enabled: bool = True) -> None:
        manifest = SkillManifest.model_validate(skill.manifest)
        if manifest.api_version != API_VERSION:
            raise ValueError(
                f"Skill {manifest.skill_id} targets API {manifest.api_version}, expected {API_VERSION}"
            )
        if manifest.skill_id in self._skills:
            raise ValueError(f"Duplicate skill ID: {manifest.skill_id}")
        if manifest.input_schema != skill.input_schema():
            raise ValueError(f"Input schema mismatch for {manifest.skill_id}")
        if manifest.output_schema != skill.output_schema():
            raise ValueError(f"Output schema mismatch for {manifest.skill_id}")
        self._skills[manifest.skill_id] = skill
        if enabled:
            self._enabled.add(manifest.skill_id)

    def get(self, skill_id: str) -> Skill:
        if skill_id not in self._enabled:
            raise KeyError(f"Skill is unavailable or disabled: {skill_id}")
        return self._skills[skill_id]

    def inspect(self, skill_id: str) -> Skill:
        if skill_id not in self._skills:
            raise KeyError(skill_id)
        return self._skills[skill_id]

    def list(self) -> tuple[SkillManifest, ...]:
        return tuple(skill.manifest for skill in self._skills.values())

    def is_enabled(self, skill_id: str) -> bool:
        return skill_id in self._enabled

    def enable(self, skill_id: str) -> None:
        if skill_id not in self._skills:
            raise KeyError(skill_id)
        self._enabled.add(skill_id)

    def disable(self, skill_id: str) -> None:
        self._enabled.discard(skill_id)

    def uninstall(self, skill_id: str) -> None:
        self._enabled.discard(skill_id)
        self._skills.pop(skill_id, None)
        if self.install_dir:
            target = (self.install_dir / skill_id).resolve()
            root = self.install_dir.resolve()
            if root not in target.parents:
                raise ValueError("Invalid skill path")
            shutil.rmtree(target, ignore_errors=True)

    def load_entry_point(self, entry_point: str) -> Skill:
        module_name, object_name = entry_point.split(":", 1)
        instance = getattr(importlib.import_module(module_name), object_name)()
        if not isinstance(instance, Skill):
            raise TypeError(f"{entry_point} is not a Skill")
        self.register(instance, enabled=False)
        return instance

    def validate_package(self, package_dir: Path) -> SkillManifest:
        manifest_path = package_dir / "manifest.json"
        manifest = SkillManifest.model_validate_json(manifest_path.read_text("utf-8"))
        if manifest.api_version != API_VERSION:
            raise ValueError("Incompatible skill API version")
        self.verifier.verify(package_dir, required=self.require_signatures)
        module_name, _, object_name = manifest.entry_point.partition(":")
        if not module_name or not object_name:
            raise ValueError("Skill entry point must use module:Object syntax")
        module_path = package_dir.joinpath(*module_name.split(".")).with_suffix(".py")
        package_path = package_dir.joinpath(*module_name.split("."), "__init__.py")
        if not module_path.is_file() and not package_path.is_file():
            raise ValueError("Skill entry point module is missing from the package")
        return manifest

    def install(self, package_dir: Path) -> SkillManifest:
        if self.install_dir is None:
            raise RuntimeError("No skill install directory configured")
        manifest = self.validate_package(package_dir)
        target = self.install_dir / manifest.skill_id
        if target.exists():
            raise FileExistsError(manifest.skill_id)
        self.install_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package_dir, target)
        return manifest

    def update(
        self, package_dir: Path, *, permission_reviewed: bool = False
    ) -> tuple[SkillManifest, tuple[str, ...]]:
        if self.install_dir is None:
            raise RuntimeError("No skill install directory configured")
        manifest = self.validate_package(package_dir)
        target = self.install_dir / manifest.skill_id
        if not target.is_dir():
            raise FileNotFoundError(manifest.skill_id)
        old_manifest = SkillManifest.model_validate_json(
            (target / "manifest.json").read_text("utf-8")
        )
        if Version(manifest.version) <= Version(old_manifest.version):
            raise ValueError("Skill update version must be newer than installed version")
        old_capabilities = {item.capability for item in old_manifest.permissions}
        new_capabilities = {item.capability for item in manifest.permissions}
        added = tuple(sorted(new_capabilities - old_capabilities))
        if added and not permission_reviewed:
            raise PermissionError(
                "Skill update adds capabilities requiring review: "
                + ", ".join(added)
            )
        staging = Path(
            tempfile.mkdtemp(prefix=f"{manifest.skill_id}-", dir=self.install_dir)
        )
        backup = target.with_name(target.name + ".previous")
        try:
            shutil.rmtree(staging)
            shutil.copytree(package_dir, staging)
            if backup.exists():
                shutil.rmtree(backup)
            target.replace(backup)
            staging.replace(target)
            shutil.rmtree(backup)
        except Exception:
            if not target.exists() and backup.exists():
                backup.replace(target)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self._enabled.discard(manifest.skill_id)
        self._skills.pop(manifest.skill_id, None)
        return manifest, added

    def load_installed(
        self,
        skill_id: str,
        *,
        timeout_seconds: float = 30,
        memory_limit_mb: int = 256,
    ) -> IsolatedSkill:
        if self.install_dir is None:
            raise RuntimeError("No skill install directory configured")
        package_dir = self.install_dir / skill_id
        manifest = self.validate_package(package_dir)
        isolated = IsolatedSkill(
            package_dir,
            manifest,
            timeout_seconds=timeout_seconds,
            memory_limit_mb=memory_limit_mb,
        )
        self.register(isolated, enabled=False)
        return isolated
