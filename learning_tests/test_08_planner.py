import pytest

from assistant.language.planner import LocalPlanner, SkillRetriever
from assistant.skills.builtin import CalculatorSkill, DateTimeSkill


class FakeProvider:
    """Return controlled model output so the test stays offline and repeatable."""

    def __init__(self, output):
        self.output = output
        self.calls = 0

    async def structured_completion(self, prompt, schema, timeout_seconds):
        self.calls += 1
        return self.output

    async def cancel(self):
        return None


def test_retriever_selects_only_skills_related_to_the_request():
    retriever = SkillRetriever(
        (CalculatorSkill.manifest, DateTimeSkill.manifest)
    )

    matches = retriever.relevant("Please calculate 10 plus 4")

    assert [manifest.skill_id for manifest in matches] == ["core.calculator"]


@pytest.mark.asyncio
async def test_planner_accepts_a_retrieved_schema_valid_skill_call():
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "core.calculator",
            "arguments": {"expression": "10 + 4"},
            "answer": None,
            "confidence": 0.95,
            "clarification": None,
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((CalculatorSkill.manifest,)),
    )

    intent = await planner.plan("calculate ten plus four")

    assert intent.name == "core.calculator"
    assert intent.arguments == {"expression": "10 + 4"}
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_obvious_gibberish_is_rejected_without_calling_the_model():
    provider = FakeProvider(None)
    planner = LocalPlanner(provider, SkillRetriever(()))

    intent = await planner.plan("zzzzzzzz")

    assert intent.name == "conversation.reject"
    assert provider.calls == 0
