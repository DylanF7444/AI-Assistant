import pytest

from assistant.context.snapshot import ContextSnapshot
from assistant.language.planner import LocalPlanner, SkillRetriever
from assistant.language.understanding import UserIntentInterpretation


class FakeProvider:
    def __init__(self, output):
        self.output = output
        self.last_prompt = ""
        self.last_schema = None

    async def structured_completion(self, prompt, schema, timeout_seconds):
        self.last_prompt = prompt
        self.last_schema = schema
        return self.output

    async def health(self):
        return {"healthy": True}

    async def cancel(self):
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
    context = ContextSnapshot(request_text="Clean up my downloads.", session_id="s1")

    interpretation = await planner.interpret(
        "Clean up my downloads.",
        context=context,
        retrieved=(),
    )

    assert interpretation.should_ask_clarification
    assert interpretation.request_type == "multi_step_goal"
    assert "Intent interpretation schema" in provider.last_prompt
    assert "Context snapshot" in provider.last_prompt
    assert provider.last_schema["properties"]["request_type"]["enum"]
