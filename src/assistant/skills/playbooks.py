from __future__ import annotations

import asyncio
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant.contracts import BackgroundMode, ResultStatus, RiskLevel, SkillCall, SkillResult
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.skills.registry import SkillRegistry
from assistant.storage.playbooks import Playbook, PlaybookSkillGuidance, PlaybookStore


class PlaybookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["save", "list", "show", "enable", "disable", "delete"]
    name: str | None = Field(default=None, max_length=200)
    instructions: str | None = Field(default=None, max_length=50_000)
    skill_ids: tuple[str, ...] | None = None

    @field_validator("name", "instructions")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("skill_ids")
    @classmethod
    def _validate_skill_ids(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if values is None:
            return None
        normalized = tuple(value.strip() for value in values if value.strip())
        if len(normalized) != len(set(normalized)):
            raise ValueError("Skill IDs cannot be repeated")
        invalid = [value for value in normalized if not re.fullmatch(r"[a-z][a-z0-9_.-]+", value)]
        if invalid:
            raise ValueError("Invalid skill IDs: " + ", ".join(invalid))
        return normalized

    @model_validator(mode="after")
    def _required_fields_are_present(self) -> PlaybookInput:
        if self.operation == "save":
            if not self.name:
                raise ValueError("Saving a playbook requires a name")
            if not self.instructions:
                raise ValueError("Saving a playbook requires workflow instructions")
        elif self.operation in {"show", "enable", "disable", "delete"} and not self.name:
            raise ValueError(f"{self.operation.title()} requires a playbook name or ID")
        elif self.instructions is not None or self.skill_ids is not None:
            raise ValueError("Only save may include instructions or skill IDs")
        return self


class PlaybookRecord(BaseModel):
    id: str
    name: str
    description: str
    version: str
    enabled: bool
    skill_ids: list[str]
    instructions: str | None = None


class PlaybookOutput(BaseModel):
    operation: str
    playbooks: list[PlaybookRecord]


class PlaybookSkill(Skill):
    input_model = PlaybookInput
    output_model = PlaybookOutput

    def __init__(self, store: PlaybookStore, registry: SkillRegistry) -> None:
        self.store = store
        self.registry = registry
        self.manifest = manifest(
            skill_id="core.playbook",
            display_name="Playbook",
            description=(
                "Save, list, inspect, enable, disable, or delete user-authored workflow "
                "instructions. Applying a playbook is handled by the planner."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            background_mode=BackgroundMode.FOREGROUND_ONLY,
        )

    def validate(self, arguments: dict) -> PlaybookInput:
        args = PlaybookInput.model_validate(arguments)
        if args.operation == "save":
            unknown = []
            for skill_id in args.skill_ids or ():
                try:
                    self.registry.inspect(skill_id)
                except KeyError:
                    unknown.append(skill_id)
            if unknown:
                raise ValueError("Unknown registered skill IDs: " + ", ".join(unknown))
        elif args.operation in {"show", "enable", "disable", "delete"}:
            assert args.name is not None
            self.store.find(args.name)
        return args

    def risk_for(self, call: SkillCall) -> RiskLevel:
        args = PlaybookInput.model_validate(call.arguments)
        return RiskLevel.MEDIUM if args.operation == "delete" else RiskLevel.LOW

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "save":
            return f'Save playbook "{args.name}"'
        if args.operation == "list":
            return "List saved playbooks"
        return f'{args.operation.title()} playbook "{args.name}"'

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if args.operation == "list":
            values = self.store.list()
            records = [self._record(item) for item in values]
            summary = (
                "No playbooks are saved."
                if not records
                else "Saved playbooks: "
                + ", ".join(
                    f"{item.name} ({'enabled' if item.enabled else 'disabled'})" for item in values
                )
            )
            return self._result(args.operation, records, summary)

        assert args.name is not None
        if args.operation == "save":
            assert args.instructions is not None
            playbook = self._save(args)
            return self._result(
                args.operation,
                [self._record(playbook, include_instructions=True)],
                f'Saved playbook "{playbook.name}" with ID {playbook.playbook_id}.',
            )

        playbook = self.store.find(args.name)
        if args.operation == "show":
            guidance = ", ".join(playbook.skill_ids) or "no specific skills"
            return self._result(
                args.operation,
                [self._record(playbook, include_instructions=True)],
                (
                    f"{playbook.name} ({'enabled' if playbook.enabled else 'disabled'}); "
                    f"skills: {guidance}.\n\n{playbook.instructions}"
                ),
            )
        if args.operation in {"enable", "disable"}:
            enabled = args.operation == "enable"
            playbook = self.store.set_enabled(playbook.playbook_id, enabled)
            return self._result(
                args.operation,
                [self._record(playbook)],
                f'{args.operation.title()}d playbook "{playbook.name}".',
            )

        self.store.delete(playbook.playbook_id)
        return self._result(
            args.operation,
            [],
            f'Deleted playbook "{playbook.name}".',
        )

    def _save(self, args: PlaybookInput) -> Playbook:
        assert args.name is not None
        assert args.instructions is not None
        try:
            existing = self.store.find(args.name)
        except KeyError:
            existing = None
        if existing is None:
            playbook_id = self._playbook_id(args.name)
            version = "1"
            description = "Saved from a user command."
            triggers = (args.name,)
            existing_skills: tuple[PlaybookSkillGuidance, ...] = ()
        else:
            playbook_id = existing.playbook_id
            version = self._next_version(existing.version)
            description = existing.description
            triggers = existing.triggers
            existing_skills = existing.skills
        skills = (
            tuple(PlaybookSkillGuidance(id=skill_id) for skill_id in args.skill_ids)
            if args.skill_ids is not None
            else existing_skills
        )
        playbook = Playbook(
            id=playbook_id,
            name=existing.name if existing is not None else args.name,
            description=description,
            version=version,
            enabled=existing.enabled if existing is not None else True,
            triggers=triggers,
            skills=skills,
            instructions=args.instructions,
        )
        return self.store.save_markdown(
            self.store.render(playbook),
            original_id=existing.playbook_id if existing is not None else None,
        )

    @staticmethod
    def _playbook_id(name: str) -> str:
        playbook_id = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        if not playbook_id or not playbook_id[0].isalpha():
            raise ValueError("Playbook name must contain a letter and produce a valid ID")
        return playbook_id

    @staticmethod
    def _next_version(version: str) -> str:
        return str(int(version) + 1) if version.isdigit() else version

    @staticmethod
    def _record(playbook: Playbook, *, include_instructions: bool = False) -> PlaybookRecord:
        return PlaybookRecord(
            id=playbook.playbook_id,
            name=playbook.name,
            description=playbook.description,
            version=playbook.version,
            enabled=playbook.enabled,
            skill_ids=list(playbook.skill_ids),
            instructions=playbook.instructions if include_instructions else None,
        )

    @staticmethod
    def _result(
        operation: str,
        records: list[PlaybookRecord],
        summary: str,
    ) -> SkillResult:
        output = PlaybookOutput(operation=operation, playbooks=records)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            display_text=summary,
            data=output.model_dump(mode="json"),
        )
