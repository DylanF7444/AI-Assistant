from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SkillExample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    utterance: str
    skill_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    requires_clarification: bool = False
    clarification: str | None = None


DEFAULT_SKILL_EXAMPLES: tuple[SkillExample, ...] = (
    SkillExample(
        utterance="clean up my downloads",
        skill_id="windows.file_operation",
        notes=(
            "Broad filesystem request; inspect and preview before moving files. "
            "Ask how to organize the folder before changing anything."
        ),
        requires_clarification=True,
        clarification="Should I organize Downloads by file type, date, or project/name?",
    ),
    SkillExample(
        utterance="move the first result to Documents",
        skill_id="windows.file_operation",
        arguments={
            "operation": "move",
            "source": "the first result",
            "destination": "documents",
        },
        notes="Contextual file result selection requires an explicit selected result.",
    ),
    SkillExample(
        utterance="organize PDFs into a folder",
        skill_id="windows.file_operation",
        notes="Requires a target folder and a preview before moving files.",
        requires_clarification=True,
        clarification="Which folder should I organize?",
    ),
    SkillExample(
        utterance="put Brave on my second monitor",
        skill_id="windows.application_window",
        arguments={
            "operation": "move_to_monitor",
            "process_name": "brave.exe",
            "monitor": 2,
        },
    ),
    SkillExample(
        utterance="get this out of the way",
        skill_id="windows.application_window",
        arguments={"operation": "minimize_focused"},
        notes="Requires focused window context.",
    ),
)
