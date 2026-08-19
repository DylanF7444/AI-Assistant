from __future__ import annotations

import asyncio
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from assistant.command_center import WorkBrief
from assistant.command_center.service import CommandCenterService
from assistant.contracts import (
    RequestResultItem,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest


class WorkBriefInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_binding_ids: tuple[UUID, ...] | None = None
    delivery: Literal["assistant", "inbox"] = "assistant"
    save: bool = False
    retention_days: int = 30


class WorkBriefOutput(BaseModel):
    brief: WorkBrief
    delivered_to: Literal["assistant", "inbox"]
    artifact_id: UUID | None = None


class WorkBriefSkill(Skill):
    input_model = WorkBriefInput
    output_model = WorkBriefOutput

    def __init__(self, command_center: CommandCenterService) -> None:
        self.command_center = command_center
        self.manifest = manifest(
            skill_id="core.work_brief",
            display_name="Work Brief",
            description=(
                "Build a bounded manual brief from explicitly configured read-only inputs, "
                "with independent permission checks and partial failure reporting."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            risk=RiskLevel.LOW,
        ).model_copy(update={"entry_point": f"{__name__}:WorkBriefSkill"})

    def permission_checks(self, call: SkillCall):
        arguments = self.validate(call.arguments)
        return self.command_center.briefing_permission_checks(
            call.permission_context,
            arguments.source_binding_ids,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        arguments = self.validate(call.arguments)
        sources = await self.command_center.collect_sources(
            call.permission_context,
            binding_ids=arguments.source_binding_ids,
        )
        if cancel_event.is_set():
            return SkillResult(
                status=ResultStatus.CANCELLED,
                summary="Work brief cancelled.",
                sensitive=True,
            )
        brief = self.command_center.build_brief(sources)
        artifact = (
            self.command_center.save_brief(brief, retention_days=arguments.retention_days)
            if arguments.save
            else None
        )
        if arguments.delivery == "inbox":
            destination = next(
                (
                    binding
                    for binding in self.command_center.destination_bindings()
                    if binding.enabled and binding.destination_id == "local.inbox"
                ),
                None,
            )
            if destination is None:
                return SkillResult(
                    status=ResultStatus.DENIED,
                    summary="Add and enable the Inbox output in Connections first.",
                    display_text=brief.content,
                    data={
                        "brief": brief.model_dump(mode="json"),
                        "delivered_to": "assistant",
                    },
                    sensitive=True,
                )
            self.command_center.deliver_local(brief, destination)

        completed = sum(
            source.status in {ResultStatus.COMPLETED, ResultStatus.PARTIAL}
            for source in brief.sources
        )
        summary = (
            "No briefing inputs are configured."
            if not brief.sources
            else f"Built a work brief from {completed} of {len(brief.sources)} inputs."
        )
        return SkillResult(
            status=brief.status,
            summary=summary,
            display_text=brief.content,
            data=WorkBriefOutput(
                brief=brief,
                delivered_to=arguments.delivery,
                artifact_id=artifact.artifact_id if artifact else None,
            ).model_dump(mode="json"),
            items=tuple(
                RequestResultItem(
                    item_id=f"brief-source-{index}",
                    title=source.display_name,
                    value={
                        "binding_id": str(source.binding_id),
                        "source_id": source.source_id,
                        "status": source.status.value,
                    },
                    kind="brief-source",
                    description=source.summary or source.error,
                    sensitive=source.sensitive,
                )
                for index, source in enumerate(brief.sources, 1)
            )
            or None,
            # Aggregated source content is transient and never belongs in audit payloads.
            sensitive=True,
        )
