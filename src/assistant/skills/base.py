from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, ValidationError

from assistant.contracts import (
    PipelineEvent,
    RiskLevel,
    SkillCall,
    SkillManifest,
    SkillResult,
)


class Skill(ABC):
    manifest: SkillManifest
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    def validate(self, arguments: dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(arguments)

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()

    def validate_result(self, result: SkillResult) -> None:
        self.output_model.model_validate(result.data)

    def required_permissions(self, call: SkillCall):
        return self.manifest.permissions

    def permission_checks(self, call: SkillCall):
        return ((call, self.required_permissions(call)),)

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return self.manifest.risk

    async def preview(self, call: SkillCall) -> str:
        arguments = self.validate(call.arguments)
        return f"{self.manifest.display_name}: {arguments.model_dump_json()}"

    @abstractmethod
    async def execute(
        self, call: SkillCall, cancel_event: asyncio.Event
    ) -> SkillResult: ...

    async def progress(self, call: SkillCall) -> AsyncIterator[PipelineEvent]:
        if False:
            yield  # pragma: no cover

    async def cancel(self, correlation_id: str) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"healthy": True, "skill_id": self.manifest.skill_id}

    async def rollback(self, token: str) -> SkillResult:
        raise NotImplementedError("This skill does not support rollback")


def validate_call(skill: Skill, call: SkillCall) -> BaseModel:
    if call.skill_id != skill.manifest.skill_id:
        raise ValidationError.from_exception_data(
            "SkillCall", [{"type": "value_error", "loc": ("skill_id",), "ctx": {"error": ValueError("Skill ID mismatch")}, "input": call.skill_id}]
        )
    return skill.validate(call.arguments)
