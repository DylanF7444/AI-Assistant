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
        utterance="Go to Slickdeals and give me the first deal",
        skill_id="web.headless_read",
        arguments={
            "url": "https://slickdeals.net",
            "max_characters": 12000,
            "max_links": 30,
            "timeout_seconds": 30,
            "wait_after_load_ms": 750,
            "extract": "deal",
            "max_items": 1,
        },
        notes=(
            "The user wants website content returned in the assistant, so render the "
            "page in the isolated browser and extract the first matching deal card."
        ),
    ),
    SkillExample(
        utterance="I need a fresh calculator window",
        skill_id="windows.launcher",
        arguments={"application": "calculator"},
        notes=(
            "Outcome-oriented wording requests a new application instance, not focus "
            "or manipulation of an existing window."
        ),
    ),
    SkillExample(
        utterance="bring calculator to the front",
        skill_id="windows.application_window",
        arguments={"operation": "focus", "process_name": "calculator.exe"},
        notes="Bringing an existing application forward is a window focus operation.",
    ),
    SkillExample(
        utterance="show the second photo result in the assistant",
        skill_id="media.present",
        arguments={
            "source": "the second result",
            "kind_hint": "image",
            "autoplay": False,
        },
        notes=(
            "In-app presentation is distinct from opening the file in its "
            "registered Windows application."
        ),
    ),
    SkillExample(
        utterance="play the meeting recording from my Music folder",
        skill_id="media.present",
        arguments={
            "query": "meeting",
            "root": "music",
            "kind_hint": "audio",
            "autoplay": True,
            "limit": 20,
        },
        notes="Autoplay is allowed only because the request explicitly says play.",
    ),
    SkillExample(
        utterance="skip ahead thirty seconds in this video",
        skill_id="media.viewer_control",
        arguments={"action": "seek_relative", "delta_seconds": 30},
        notes="This controls only the active in-app viewer, not global Windows media.",
    ),
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
