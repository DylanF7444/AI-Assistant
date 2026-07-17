from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLAYBOOK_ID_PATTERN = r"^[a-z][a-z0-9_.-]+$"


class PlaybookSkillGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    skill_id: str = Field(alias="id", pattern=PLAYBOOK_ID_PATTERN)
    instructions: str = Field(default="", max_length=2_000)

    @field_validator("skill_id", "instructions")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


class Playbook(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    playbook_id: str = Field(alias="id", pattern=PLAYBOOK_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    version: str = Field(default="1", min_length=1, max_length=50)
    enabled: bool = True
    triggers: tuple[str, ...] = ()
    skills: tuple[PlaybookSkillGuidance, ...] = ()
    instructions: str = Field(min_length=1, max_length=50_000)
    source_path: Path | None = Field(default=None, exclude=True)

    @field_validator("version", mode="before")
    @classmethod
    def _stringify_version(cls, value) -> str:
        if isinstance(value, bool) or value is None:
            raise ValueError("Playbook version must be text or a number")
        return str(value)

    @field_validator("playbook_id", "name", "description", "version", "instructions")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("triggers", mode="before")
    @classmethod
    def _normalize_triggers(cls, value):
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return value

    @field_validator("triggers")
    @classmethod
    def _validate_triggers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values if value.strip())
        if any(len(value) > 200 for value in normalized):
            raise ValueError("Playbook triggers must be 200 characters or fewer")
        return normalized

    @field_validator("skills", mode="before")
    @classmethod
    def _normalize_skills(cls, value):
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple({"id": item} if isinstance(item, str) else item for item in value)
        return value

    @model_validator(mode="after")
    def _skill_ids_are_unique(self) -> Playbook:
        skill_ids = [item.skill_id for item in self.skills]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("A playbook cannot list the same skill ID more than once")
        if "core.playbook" in skill_ids:
            raise ValueError("A playbook cannot invoke playbook management")
        return self

    @property
    def content_hash(self) -> str:
        payload = self.model_dump(mode="json", by_alias=True, exclude={"source_path"})
        return hashlib.sha256(
            yaml.safe_dump(payload, sort_keys=True, allow_unicode=True).encode("utf-8")
        ).hexdigest()

    @property
    def skill_ids(self) -> tuple[str, ...]:
        return tuple(item.skill_id for item in self.skills)


class PlaybookStore:
    SUPPORTED_SUFFIXES = {".md", ".markdown"}
    MAX_FILE_BYTES = 128 * 1024

    EXAMPLE = """---
id: prepare-release
name: Prepare a release
description: Check whether a project is ready to release.
version: 1
enabled: true
triggers:
  - prepare a release
  - check release readiness
skills:
  - id: core.datetime
    instructions: Use only when the checklist needs the current date or time.
---

# Workflow

1. Identify the project and the user's success criteria.
2. Inspect available context before proposing changes.
3. Run read-only checks first.
4. Summarize failures and ask before making sensitive changes.
5. Finish with a concise readiness checklist.
"""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def parse_text(cls, content: str, *, source_path: Path | None = None) -> Playbook:
        if len(content.encode("utf-8")) > cls.MAX_FILE_BYTES:
            raise ValueError("Playbook files must be 128 KB or smaller")
        content = content.lstrip("\ufeff")
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("Playbook must start with YAML frontmatter delimited by ---")
        closing_index = next(
            (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
            None,
        )
        if closing_index is None:
            raise ValueError("Playbook YAML frontmatter is missing its closing ---")
        raw_header = "\n".join(lines[1:closing_index])
        try:
            header = yaml.safe_load(raw_header)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid playbook YAML: {exc}") from exc
        if not isinstance(header, dict):
            raise ValueError("Playbook YAML frontmatter must be a mapping")
        instructions = "\n".join(lines[closing_index + 1 :]).strip()
        payload = dict(header)
        payload["instructions"] = instructions
        payload["source_path"] = source_path
        return Playbook.model_validate(payload)

    @classmethod
    def parse_file(cls, path: Path) -> Playbook:
        path = path.resolve()
        if path.suffix.casefold() not in cls.SUPPORTED_SUFFIXES:
            raise ValueError("Playbooks must be Markdown files")
        if not path.is_file():
            raise ValueError("Playbook file does not exist")
        if path.stat().st_size > cls.MAX_FILE_BYTES:
            raise ValueError("Playbook files must be 128 KB or smaller")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Playbook files must use UTF-8 text encoding") from exc
        return cls.parse_text(content, source_path=path)

    @staticmethod
    def render(playbook: Playbook) -> str:
        header = {
            "id": playbook.playbook_id,
            "name": playbook.name,
            "description": playbook.description,
            "version": playbook.version,
            "enabled": playbook.enabled,
            "triggers": list(playbook.triggers),
            "skills": [
                {
                    "id": guidance.skill_id,
                    "instructions": guidance.instructions,
                }
                for guidance in playbook.skills
            ],
        }
        yaml_text = yaml.safe_dump(
            header,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ).strip()
        return f"---\n{yaml_text}\n---\n\n{playbook.instructions.strip()}\n"

    def scan(self) -> tuple[tuple[Playbook, ...], dict[str, str]]:
        if not self.root.is_dir():
            return (), {}
        values: dict[str, Playbook] = {}
        errors: dict[str, str] = {}
        paths = sorted(
            (
                path
                for path in self.root.rglob("*")
                if path.is_file() and path.suffix.casefold() in self.SUPPORTED_SUFFIXES
            ),
            key=lambda path: str(path).casefold(),
        )
        for path in paths:
            try:
                playbook = self.parse_file(path)
            except Exception as exc:
                errors[str(path)] = str(exc)
                continue
            if playbook.playbook_id in values:
                errors[str(path)] = f"Duplicate playbook ID: {playbook.playbook_id}"
                continue
            values[playbook.playbook_id] = playbook
        return (
            tuple(sorted(values.values(), key=lambda item: item.name.casefold())),
            errors,
        )

    def list(self, *, enabled_only: bool = False) -> tuple[Playbook, ...]:
        values, _ = self.scan()
        if enabled_only:
            return tuple(item for item in values if item.enabled)
        return values

    def errors(self) -> dict[str, str]:
        return self.scan()[1]

    def get(self, playbook_id: str) -> Playbook:
        for playbook in self.list():
            if playbook.playbook_id == playbook_id:
                return playbook
        raise KeyError(playbook_id)

    def find(self, reference: str) -> Playbook:
        reference = reference.strip().casefold()
        if not reference:
            raise KeyError(reference)
        values = self.list()
        id_match = next(
            (item for item in values if item.playbook_id.casefold() == reference),
            None,
        )
        if id_match is not None:
            return id_match
        name_matches = [item for item in values if item.name.casefold() == reference]
        if len(name_matches) == 1:
            return name_matches[0]
        if len(name_matches) > 1:
            raise ValueError(
                f'Multiple playbooks are named "{reference}"; use the exact playbook ID.'
            )
        raise KeyError(reference)

    def import_file(self, path: Path) -> Playbook:
        playbook = self.parse_file(path)
        target = self._canonical_path(playbook.playbook_id)
        if target.exists() or any(item.playbook_id == playbook.playbook_id for item in self.list()):
            raise FileExistsError(playbook.playbook_id)
        return self._write(playbook, target)

    def save_markdown(self, content: str, *, original_id: str | None = None) -> Playbook:
        existing = self.get(original_id) if original_id is not None else None
        playbook = self.parse_text(content)
        conflict = next(
            (
                item
                for item in self.list()
                if item.playbook_id == playbook.playbook_id
                and (existing is None or item.playbook_id != existing.playbook_id)
            ),
            None,
        )
        if conflict is not None:
            raise FileExistsError(playbook.playbook_id)
        target = (
            existing.source_path
            if existing is not None and existing.playbook_id == playbook.playbook_id
            else self._canonical_path(playbook.playbook_id)
        )
        assert target is not None
        if target.exists() and (existing is None or target.resolve() != existing.source_path):
            raise FileExistsError(playbook.playbook_id)
        saved = self._write(playbook, target)
        if (
            existing is not None
            and existing.source_path is not None
            and existing.source_path.resolve() != target.resolve()
        ):
            self._safe_path(existing.source_path).unlink(missing_ok=True)
        return saved

    def set_enabled(self, playbook_id: str, enabled: bool) -> Playbook:
        playbook = self.get(playbook_id)
        assert playbook.source_path is not None
        return self._write(playbook.model_copy(update={"enabled": enabled}), playbook.source_path)

    def delete(self, playbook_id: str) -> bool:
        try:
            playbook = self.get(playbook_id)
        except KeyError:
            return False
        assert playbook.source_path is not None
        self._safe_path(playbook.source_path).unlink(missing_ok=True)
        return True

    def clear(self) -> None:
        if not self.root.is_dir():
            return
        for path in self.root.rglob("*"):
            if path.is_file() and path.suffix.casefold() in self.SUPPORTED_SUFFIXES:
                self._safe_path(path).unlink(missing_ok=True)

    def _canonical_path(self, playbook_id: str) -> Path:
        if not re.fullmatch(PLAYBOOK_ID_PATTERN, playbook_id):
            raise ValueError("Invalid playbook ID")
        return self._safe_path(self.root / f"{playbook_id}.md")

    def _safe_path(self, path: Path) -> Path:
        root = self.root.resolve()
        target = path.resolve()
        if root != target.parent and root not in target.parents:
            raise ValueError("Invalid playbook path")
        return target

    def _write(self, playbook: Playbook, target: Path) -> Playbook:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._safe_path(target)
        temporary = self._safe_path(target.with_name(f".{target.name}.{uuid4().hex}.tmp"))
        try:
            temporary.write_text(self.render(playbook), encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return self.parse_file(target)
