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
        utterance="Check my calendar",
        skill_id="integration.google_calendar",
        arguments={
            "operation": "list_events",
            "calendar_id": "primary",
            "max_results": 10,
            "single_events": True,
            "order_by": "startTime",
        },
        notes="A request to check the calendar reads upcoming events from the primary calendar.",
    ),
    SkillExample(
        utterance="Add dentist tomorrow at 3 PM to my calendar",
        skill_id="integration.google_calendar",
        arguments={
            "operation": "quick_add",
            "calendar_id": "primary",
            "quick_add_text": "dentist tomorrow at 3 PM",
        },
        notes=(
            "Natural-language event creation should use Calendar quick-add when the "
            "request already contains the title and timing."
        ),
    ),
    SkillExample(
        utterance="Find the quarterly planning meeting on my calendar",
        skill_id="integration.google_calendar",
        arguments={
            "operation": "list_events",
            "calendar_id": "primary",
            "query": "quarterly planning meeting",
            "max_results": 50,
            "single_events": True,
        },
        notes="Searching the calendar maps the descriptive event text to the query field.",
    ),
    SkillExample(
        utterance="List my Google calendars",
        skill_id="integration.google_calendar",
        arguments={"operation": "list_calendars", "max_results": 50},
    ),
    SkillExample(
        utterance=(
            r"Upload C:\Reports\final.pdf to Google Drive folder 1AbC and name it final.pdf"
        ),
        skill_id="integration.google_drive",
        arguments={
            "operation": "upload",
            "local_path": r"C:\Reports\final.pdf",
            "parent_id": "1AbC",
            "name": "final.pdf",
        },
        notes="Uploads name one explicit local source and exact Drive parent; overwrite is excluded.",
    ),
    SkillExample(
        utterance="Create an Outlook draft to alex@example.com about the launch review",
        skill_id="integration.outlook_mail",
        arguments={
            "operation": "create_draft",
            "recipients": ["alex@example.com"],
            "subject": "Launch review",
            "body": "Please review the launch plan.",
        },
        notes="Creating a draft never implies sending it; sending is a separate confirmed action.",
    ),
    SkillExample(
        utterance="Send Outlook draft AAMk123",
        skill_id="integration.outlook_mail",
        arguments={"operation": "send_draft", "message_id": "AAMk123"},
        notes="Only an exact existing draft ID can be sent, after a high-risk confirmation.",
    ),
    SkillExample(
        utterance="Post deployment is complete to Slack channel C012345",
        skill_id="integration.slack",
        arguments={
            "operation": "post_message",
            "channel_id": "C012345",
            "text": "Deployment is complete",
        },
        notes="Slack writes require an exact channel ID and show the exact message before sending.",
    ),
    SkillExample(
        utterance="Create a Notion page called Launch Notes under page abc123",
        skill_id="integration.notion",
        arguments={
            "operation": "create_page",
            "resource_id": "abc123",
            "title": "Launch Notes",
            "content": "Launch notes",
        },
        notes="Notion creation is limited to one exact parent and bounded initial content.",
    ),
    SkillExample(
        utterance="Create a GitHub issue in acme/widgets titled Startup crash",
        skill_id="integration.github",
        arguments={
            "operation": "create_issue",
            "owner": "acme",
            "repo": "widgets",
            "title": "Startup crash",
            "body": "The application crashes during startup.",
        },
        notes="Issue creation targets one explicit repository and is confirmed before mutation.",
    ),
    SkillExample(
        utterance=(
            r"When a PDF is created under C:\Incoming, run windows.notification with "
            "title New PDF and message A PDF arrived"
        ),
        skill_id="automation.schedule",
        arguments={
            "operation": "create_action",
            "name": "New PDF notification",
            "skill_id": "windows.notification",
            "arguments": {"title": "New PDF", "message": "A PDF arrived"},
            "event": {
                "observation_type": "file",
                "target": r"C:\Incoming",
                "events": ["created"],
                "minimum_interval_seconds": 30,
            },
        },
        notes="Event schedules freeze one typed action and require exact monitor and action scopes.",
    ),
    SkillExample(
        utterance="Give me a briefing on the latest news about AI and the economy",
        skill_id="web.news_search",
        arguments={
            "query": "latest news artificial intelligence economy",
            "count": 5,
        },
        notes=(
            "Current-news requests require live web results. Search first, then use "
            "the observed result data to write the requested briefing."
        ),
    ),
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
        utterance="summarize this local PDF",
        skill_id="documents.read",
        arguments={"operation": "summarize", "paths": ["the selected file"]},
        notes="Reading a document body is separately permissioned from filename search.",
    ),
    SkillExample(
        utterance="index document contents in my project folder",
        skill_id="documents.content_search",
        arguments={"operation": "index", "root": "the selected project folder"},
        notes="Persistent content indexing requires an explicit root and distinct retention grant.",
    ),
    SkillExample(
        utterance="search my indexed project documents for launch risks",
        skill_id="documents.content_search",
        arguments={
            "operation": "search",
            "root": "the selected project folder",
            "query": "launch risks",
        },
    ),
    SkillExample(
        utterance="read the text in this screenshot",
        skill_id="vision.understand_image",
        arguments={"operation": "ocr", "path": "the selected screenshot"},
        notes="Visual analysis only reads an explicitly selected image; it never captures a screen.",
    ),
    SkillExample(
        utterance="explain this error dialog",
        skill_id="vision.understand_image",
        arguments={"operation": "explain_error", "path": "the selected screenshot"},
        notes="Guidance is descriptive and cannot click, type, or expand input-control authority.",
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
