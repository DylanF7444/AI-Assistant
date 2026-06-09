import pytest

from assistant.language.planner import LocalPlanner, SkillRetriever
from assistant.skills.builtin import CalculatorSkill, DateTimeSkill


class FakeProvider:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    async def structured_completion(self, prompt, schema, timeout_seconds):
        output = (
            self.output[self.calls]
            if isinstance(self.output, list)
            else self.output
        )
        self.calls += 1
        return output

    async def health(self):
        return {"healthy": True}

    async def cancel(self):
        pass


@pytest.mark.asyncio
async def test_planner_accepts_only_retrieved_schema_valid_call():
    manifests = (CalculatorSkill.manifest, DateTimeSkill.manifest)
    planner = LocalPlanner(
        FakeProvider(
            {
                "disposition": "skill",
                "skill_id": "core.calculator",
                "arguments": {"expression": "2 + 2"},
                "answer": None,
                "confidence": 0.9,
                "clarification": None,
            }
        ),
        SkillRetriever(manifests),
    )
    intent = await planner.plan("calculate two plus two")
    assert intent.name == "core.calculator"


@pytest.mark.asyncio
async def test_planner_rejects_hallucinated_skill():
    provider = FakeProvider(
        [
            {
                "disposition": "skill",
                "skill_id": "shell.run",
                "arguments": {"command": "format c:"},
                "answer": None,
                "confidence": 1,
                "clarification": None,
            },
            {
                "disposition": "reject",
                "skill_id": "",
                "arguments": {},
                "answer": "I cannot do that.",
                "confidence": 1,
                "clarification": None,
            },
        ]
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((CalculatorSkill.manifest,)),
    )
    intent = await planner.plan("calculate and run a shell command")
    assert intent.name == "conversation.reject"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_planner_uses_answer_when_model_mislabels_it_as_a_skill():
    planner = LocalPlanner(
        FakeProvider(
            {
                "disposition": "skill",
                "skill_id": "get_capital_of_france",
                "arguments": {},
                "answer": "Paris is the capital of France.",
                "confidence": 0.98,
                "clarification": None,
            }
        ),
        SkillRetriever((CalculatorSkill.manifest,)),
    )
    intent = await planner.plan("What is the capital of France?")
    assert intent.name == "conversation.answer"
    assert intent.arguments["text"].startswith("Paris")


@pytest.mark.asyncio
async def test_planner_answers_when_no_skill_is_relevant():
    planner = LocalPlanner(
        FakeProvider(
            {
                "disposition": "answer",
                "skill_id": "",
                "arguments": {},
                "answer": "Paris is the capital of France.",
                "confidence": 0.98,
                "clarification": None,
            }
        ),
        SkillRetriever((CalculatorSkill.manifest,)),
    )
    intent = await planner.plan("What is the capital of France?")
    assert intent.name == "conversation.answer"
    assert intent.arguments["text"].startswith("Paris")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["zzzzzzzz", "...?!", "um uh"])
async def test_planner_rejects_obvious_gibberish_without_calling_provider(text):
    provider = FakeProvider(None)
    planner = LocalPlanner(provider, SkillRetriever(()))
    intent = await planner.plan(text)
    assert intent.name == "conversation.reject"
    assert provider.calls == 0


def test_retriever_does_not_match_substrings_inside_skill_descriptions():
    retriever = SkillRetriever((CalculatorSkill.manifest, DateTimeSkill.manifest))
    assert retriever.relevant("blorp snazzle") == ()
