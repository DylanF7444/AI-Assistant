import pytest

from assistant.bootstrap import build_container
from assistant.contracts import Intent, ResponseType
from assistant.language.planner import PlannerDecision
from assistant.language.understanding import UserIntentInterpretation


class ScenarioPlanner:
    def __init__(self):
        self.calls = []

    async def interpret(self, text, *, context, retrieved):
        self.calls.append((text, context, retrieved))
        folded = text.casefold()
        clarification = _scenario_clarification(folded)
        request_type = "multi_step_goal" if clarification else "local_action"
        return UserIntentInterpretation(
            user_goal=_scenario_goal(folded),
            request_type=request_type,
            explicit_requests=[text],
            missing_information=[clarification] if clarification else [],
            relevant_context_keys=_relevant_context_keys(folded),
            should_ask_clarification=bool(clarification),
            clarification_question=clarification,
            confidence=0.9,
        )

    async def plan_from_interpretation(self, text, *, interpretation, context, retrieved):
        if "usual browser" in text.casefold() and "memory.preferences" in context.keys():
            return PlannerDecision(
                intent=Intent(
                    name="conversation.answer",
                    arguments={"text": "I found your browser preference."},
                    confidence=0.9,
                    source="llm",
                )
            )
        return PlannerDecision(
            intent=Intent(
                name="conversation.answer",
                arguments={"text": "I need more detail before acting."},
                confidence=0.8,
                source="llm",
            )
        )

    async def cancel(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "question"),
    [
        (
            "Clean up my downloads.",
            "Should I organize Downloads by file type, date, or project/name?",
        ),
        (
            "Prepare for my meeting.",
            "Which meeting should I help prepare for?",
        ),
        (
            "Find what I was working on yesterday.",
            "Should I search recent assistant history, files, or a specific project?",
        ),
        (
            "Organize these files by project.",
            "Which files or folder should I organize?",
        ),
        (
            "Make this note into tasks.",
            "What note or source text should I turn into tasks?",
        ),
        (
            "Turn that down.",
            'What should I turn down?',
        ),
        (
            "Compare these options.",
            "Which options should I compare?",
        ),
        (
            "Fix this.",
            'What should I fix, or what does "this" refer to?',
        ),
    ],
)
async def test_broad_goal_scenarios_ask_for_missing_runtime_context(
    settings,
    utterance,
    question,
):
    container = build_container(settings)
    container.orchestrator.planner = ScenarioPlanner()

    response, outcome = await container.orchestrator.handle_text(utterance)

    assert response.response_type == ResponseType.CLARIFICATION
    assert response.display_text == question
    assert outcome.result is None


@pytest.mark.asyncio
async def test_usual_browser_scenario_uses_explicit_memory_context(settings):
    container = build_container(settings)
    container.orchestrator.planner = ScenarioPlanner()
    container.memory_store.remember(
        type="preference",
        key="browser",
        value="User prefers Brave for browsing.",
        source="user",
        confidence=1,
    )

    response, outcome = await container.orchestrator.handle_text("Use my usual browser.")

    assert response.response_type == ResponseType.ANSWER
    assert response.display_text == "I found your browser preference."
    assert outcome.result is None


@pytest.mark.asyncio
async def test_broad_goal_context_omits_unpermitted_sensitive_sources(settings):
    container = build_container(settings)
    planner = ScenarioPlanner()
    container.orchestrator.planner = planner

    await container.orchestrator.handle_text("Fix this.")

    context = planner.calls[0][1]
    keys = set(context.keys())
    assert "clipboard.contents" not in keys
    assert "screen.capture" not in keys
    assert "browser.page_content" not in keys
    assert "browser.current_url" not in keys
    assert all("contents" not in key for key in keys if key.startswith("files."))


def _scenario_goal(folded: str) -> str:
    if "downloads" in folded or "organize" in folded:
        return "Organize files safely"
    if "meeting" in folded:
        return "Prepare for a meeting"
    if "yesterday" in folded:
        return "Find recent work"
    if "note" in folded:
        return "Turn a note into tasks"
    if "usual browser" in folded:
        return "Use the preferred browser"
    if "turn that down" in folded:
        return "Reduce an ambiguous target"
    if "compare" in folded:
        return "Compare options"
    return "Resolve an ambiguous issue"


def _scenario_clarification(folded: str) -> str | None:
    if "clean up" in folded and "downloads" in folded:
        return "Should I organize Downloads by file type, date, or project/name?"
    if "meeting" in folded:
        return "Which meeting should I help prepare for?"
    if "yesterday" in folded:
        return "Should I search recent assistant history, files, or a specific project?"
    if "organize" in folded and "files" in folded:
        return "Which files or folder should I organize?"
    if "note" in folded and "tasks" in folded:
        return "What note or source text should I turn into tasks?"
    if "turn that down" in folded:
        return "What should I turn down?"
    if "compare" in folded:
        return "Which options should I compare?"
    if "fix this" in folded:
        return 'What should I fix, or what does "this" refer to?'
    return None


def _relevant_context_keys(folded: str) -> list[str]:
    if "downloads" in folded or "files" in folded:
        return ["files.known_folders", "permissions.file_policy"]
    if "usual browser" in folded:
        return ["memory.preferences"]
    if "fix this" in folded:
        return ["desktop.focused_application"]
    return []
