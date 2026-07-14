import pytest

from assistant.context.snapshot import ContextSnapshot
from assistant.language.planner import LocalPlanner, SkillRetriever
from assistant.language.understanding import UserIntentInterpretation
from assistant.skills.builtin import LauncherSkill


class FakeProvider:
    def __init__(self, output):
        self.output = output
        self.last_prompt = ""
        self.last_schema = None
        self.calls = 0

    async def structured_completion(self, prompt, schema, timeout_seconds):
        self.last_prompt = prompt
        self.last_schema = schema
        self.calls += 1
        return self.output

    async def health(self):
        return {"healthy": True}

    async def cancel(self):
        pass


class FakeApplications:
    pass


def test_user_intent_interpretation_validates_contract():
    interpretation = UserIntentInterpretation(
        user_goal="Organize files in the Downloads folder",
        request_type="multi_step_goal",
        explicit_requests=["clean up downloads"],
        missing_information=["preferred organization method"],
        constraints=["preview before moving files"],
        should_ask_clarification=True,
        clarification_question=("Should I organize Downloads by file type, date, or project/name?"),
        confidence=0.86,
    )

    assert interpretation.request_type == "multi_step_goal"
    assert interpretation.should_ask_clarification
    assert interpretation.confidence == 0.86


@pytest.mark.asyncio
async def test_local_planner_interprets_request_before_skill_selection():
    provider = FakeProvider(
        {
            "user_goal": "Organize files in the Downloads folder",
            "request_type": "multi_step_goal",
            "explicit_requests": ["clean up my downloads"],
            "implicit_needs": ["inspect folder contents", "avoid deleting files"],
            "missing_information": ["preferred organization method"],
            "assumptions": [],
            "constraints": ["preview before moving files"],
            "relevant_context_keys": ["known_folders.downloads", "file_policy"],
            "risk_notes": ["moving files changes filesystem state"],
            "success_criteria": ["files are grouped predictably"],
            "should_ask_clarification": True,
            "clarification_question": (
                "Should I organize Downloads by file type, date, or project/name?"
            ),
            "confidence": 0.86,
        }
    )
    planner = LocalPlanner(provider, SkillRetriever(()))
    context = ContextSnapshot(request_text="Help me sort this workspace.", session_id="s1")

    interpretation = await planner.interpret(
        "Help me sort this workspace.",
        context=context,
        retrieved=(),
    )

    assert interpretation.should_ask_clarification
    assert interpretation.request_type == "multi_step_goal"
    assert "Intent interpretation schema" in provider.last_prompt
    assert "Context snapshot" in provider.last_prompt
    assert provider.last_schema["properties"]["request_type"]["enum"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "question"),
    [
        (
            "Clean up my Downloads.",
            "Should I organize Downloads by file type, date, or project/name?",
        ),
        ("Prepare for my meeting.", "Which meeting should I help prepare for?"),
        (
            "Find what I was working on yesterday.",
            "Should I search recent assistant history, files, or a specific project?",
        ),
        ("Organize these files by project.", "Which files or folder should I organize?"),
        ("Make this note into tasks.", "What note or source text should I turn into tasks?"),
        ("Turn that down.", "What should I turn down?"),
        ("Compare these options.", "Which options should I compare?"),
        ("Fix this.", 'What should I fix, or what does "this" refer to?'),
        ("Use my usual browser.", "What website should I open in your usual browser?"),
    ],
)
async def test_known_ambiguous_goals_do_not_depend_on_model_output(utterance, question):
    provider = FakeProvider(None)
    planner = LocalPlanner(provider, SkillRetriever(()))

    interpretation = await planner.interpret(
        utterance,
        context=ContextSnapshot(request_text=utterance, session_id="s1"),
        retrieved=(),
    )

    assert provider.calls == 0
    assert interpretation.should_ask_clarification
    assert interpretation.clarification_question == question


@pytest.mark.asyncio
async def test_local_planner_grounds_unsupported_label_when_a_skill_matches():
    provider = FakeProvider(
        {
            "user_goal": "I need a blank notepad",
            "request_type": "unsupported",
            "confidence": 0.87,
        }
    )
    launcher = LauncherSkill(FakeApplications(), resolver=lambda name: "unused.exe")
    planner = LocalPlanner(provider, SkillRetriever((launcher.manifest,)))

    interpretation = await planner.interpret(
        "I need a blank notepad",
        context=ContextSnapshot(request_text="I need a blank notepad", session_id="s1"),
        retrieved=(),
    )

    assert interpretation.request_type == "local_action"
    assert interpretation.explicit_requests == ["I need a blank notepad"]
