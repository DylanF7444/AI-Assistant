from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WorkflowTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    title: str
    description: str
    triggers: tuple[str, ...] = Field(default_factory=tuple)
    steps: tuple[str, ...] = Field(default_factory=tuple)
    clarification: str | None = None


DEFAULT_WORKFLOW_TEMPLATES: tuple[WorkflowTemplate, ...] = (
    WorkflowTemplate(
        template_id="organize_folder",
        title="Organize a folder",
        description="Inspect a folder and preview a safe organization plan before moving files.",
        triggers=("clean up downloads", "organize folder", "sort these files"),
        steps=(
            "Identify the target folder or selected files.",
            "Summarize file types, dates, and candidate groups without reading contents.",
            "Ask how the user wants the files organized when the rule is unclear.",
            "Prepare file moves only after the user approves the preview.",
        ),
        clarification="Should I organize by file type, date, or project/name?",
    ),
    WorkflowTemplate(
        template_id="prepare_meeting",
        title="Prepare for a meeting",
        description="Gather available context and ask for the meeting when calendar integration is unavailable.",
        triggers=("prepare for my meeting", "help me prep", "meeting notes"),
        steps=(
            "Identify the meeting or ask which one.",
            "Use available notes, files, and memories only when permitted.",
            "Summarize goals, questions, and follow-up tasks.",
        ),
        clarification="Which meeting should I help prepare for?",
    ),
    WorkflowTemplate(
        template_id="summarize_document",
        title="Summarize a document",
        description="Summarize user-provided or explicitly selected text or files.",
        triggers=("summarize this", "make this note shorter", "what does this document say"),
        steps=(
            "Identify the text, selected file, or approved document source.",
            "Ask for the source if no permitted content is available.",
            "Return a concise summary and any action items.",
        ),
        clarification="What text or document should I summarize?",
    ),
    WorkflowTemplate(
        template_id="troubleshoot_app",
        title="Troubleshoot an application",
        description="Use open app and process context to diagnose a local app issue without unsafe assumptions.",
        triggers=("fix this", "app is stuck", "troubleshoot this app"),
        steps=(
            "Identify the focused or named application.",
            "Check visible app/process context when allowed.",
            "Suggest safe next steps or prepare a supported action.",
        ),
        clarification='What should I fix, or which app does "this" refer to?',
    ),
    WorkflowTemplate(
        template_id="find_recent_work",
        title="Find recent work",
        description="Use session history, known folders, and approved file search to find recent work.",
        triggers=("what was I working on yesterday", "find recent work", "where is my report"),
        steps=(
            "Determine the timeframe and scope.",
            "Use recent session context and approved file search locations.",
            "Ask for scope when the target is ambiguous.",
        ),
        clarification="Should I search recent assistant history, files, or a specific project?",
    ),
    WorkflowTemplate(
        template_id="draft_message",
        title="Draft a message",
        description="Draft or rewrite a message from user-provided content and constraints.",
        triggers=("draft a message", "rewrite this", "make this sound better"),
        steps=(
            "Identify the audience, tone, and source text.",
            "Ask for missing recipient, tone, or content.",
            "Draft without sending unless a separate explicit send action is available.",
        ),
        clarification="Who is the message for, and what should it say?",
    ),
)
