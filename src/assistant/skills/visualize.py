from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from assistant.contracts import (
    BackgroundMode,
    ResultStatus,
    SkillCall,
    SkillError,
    SkillResult,
    VisualArtifact,
    VisualSpec,
)
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.visualization import parse_visual_request

_VISUAL_SPEC_ADAPTER = TypeAdapter(VisualSpec)


class VisualizeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    alt_text: str | None = Field(default=None, max_length=2_000)
    spec: VisualSpec | None = None
    request: str | None = Field(default=None, min_length=1, max_length=20_000)

    @field_validator("title", "alt_text")
    @classmethod
    def _text_is_safe(cls, value: str | None) -> str | None:
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("Visualization text cannot contain control characters")
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Visualization text cannot be blank")
        return stripped

    @field_validator("request")
    @classmethod
    def _request_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(character) < 32 and character not in "\t\r\n" for character in value):
            raise ValueError("Visualization request cannot contain control characters")
        stripped = value.strip()
        if not stripped:
            raise ValueError("Visualization request cannot be blank")
        return stripped

    @model_validator(mode="after")
    def _request_or_spec_is_required(self) -> VisualizeInput:
        if (self.spec is None) == (self.request is None):
            raise ValueError("Provide exactly one of request or spec")
        return self


class VisualizeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact: VisualArtifact | None = None


class VisualizeSkill(Skill):
    input_model = VisualizeInput
    output_model = VisualizeOutput
    manifest = manifest(
        skill_id="core.visualize",
        display_name="Visualization",
        description=(
            "Create a local chart, flowchart, timeline, or table from ordinary typed or "
            "dictated language, or from a precise structured specification, and show it in "
            "the Assistant results window."
        ),
        category="Visualization",
        input_model=input_model,
        output_model=output_model,
        safe_to_retry=True,
        background_mode=BackgroundMode.FOREGROUND_ONLY,
    )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        spec = args.spec
        title = args.title
        if spec is None:
            parsed = parse_visual_request(args.request or "")
            if parsed is None or parsed.spec is None:
                message = (
                    parsed.clarification
                    if parsed is not None and parsed.clarification
                    else "Should this be a chart, flowchart, timeline, or table?"
                )
                return SkillResult(
                    status=ResultStatus.DENIED,
                    summary=message,
                    display_text=message,
                    data=VisualizeOutput().model_dump(mode="json"),
                    error=SkillError(
                        code="clarification_required",
                        message=message,
                        recoverable=True,
                    ),
                )
            spec = _VISUAL_SPEC_ADAPTER.validate_python(parsed.spec)
            title = title or parsed.title
        assert spec is not None
        title = title or _default_title(spec)
        artifact = VisualArtifact(
            title=title,
            alt_text=_bounded_alt_text(args.alt_text or _default_alt_text(title, spec)),
            spec=spec,
        )
        visual_name = spec.kind.replace("flowchart", "flow chart")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f'Created a {visual_name} titled "{title}".',
            display_text=artifact.alt_text,
            speech_text=f"I created a {visual_name} titled {title}.",
            data=VisualizeOutput(artifact=artifact).model_dump(mode="json"),
            visual_artifact=artifact,
        )


def _default_title(spec: VisualSpec) -> str:
    return {
        "chart": "Chart",
        "flowchart": "Flowchart",
        "timeline": "Timeline",
        "table": "Table",
    }[spec.kind]


def _default_alt_text(title: str, spec: VisualSpec) -> str:
    if spec.kind == "chart":
        pairs = ", ".join(
            f"{label}: {value:g}" for label, value in zip(spec.labels, spec.values, strict=True)
        )
        return f'{spec.chart_type.title()} chart titled "{title}". {pairs}.'
    if spec.kind == "flowchart":
        labels = ", ".join(node.label for node in spec.nodes)
        return f'Flowchart titled "{title}". Steps: {labels}.'
    if spec.kind == "timeline":
        events = ", ".join(f"{event.when}: {event.label}" for event in spec.events)
        return f'Timeline titled "{title}". Events: {events}.'
    columns = ", ".join(spec.columns)
    return f'Table titled "{title}" with columns: {columns}. {len(spec.rows)} data rows.'


def _bounded_alt_text(value: str) -> str:
    return value if len(value) <= 2_000 else value[:1_997].rstrip() + "..."
