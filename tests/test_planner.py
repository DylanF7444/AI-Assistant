import pytest
from jsonschema import ValidationError

from assistant.language.planner import InvalidPlannerResponse, LocalPlanner, SkillRetriever
from assistant.skills.builtin import CalculatorSkill, DateTimeSkill, LauncherSkill, TimerSkill
from assistant.storage.database import Database
from assistant.storage.knowledge import KnowledgeBaseStore


class FakeProvider:
    def __init__(self, output):
        self.output = output
        self.calls = 0
        self.last_prompt = ""
        self.last_timeout_seconds = None

    async def structured_completion(self, prompt, schema, timeout_seconds):
        self.last_prompt = prompt
        self.last_timeout_seconds = timeout_seconds
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


class FakeApplications:
    async def launch(self, path):
        pass

    async def focus(self, name):
        return True

    async def minimize(self, name):
        return True

    async def maximize(self, name):
        return True

    async def close(self, name):
        return True


@pytest.mark.asyncio
async def test_planner_accepts_only_retrieved_schema_valid_call():
    manifests = (CalculatorSkill.manifest, DateTimeSkill.manifest)
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "core.calculator",
            "arguments": {"expression": "2 + 2"},
            "answer": None,
            "confidence": 0.9,
            "clarification": None,
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever(manifests),
    )
    intent = await planner.plan("calculate two plus two")
    assert intent.name == "core.calculator"
    assert "planning and conversational intelligence for Local Assistant" in (
        provider.last_prompt
    )
    assert "Registered skills are your only action capabilities" in provider.last_prompt
    assert '"id":"core.calculator"' in provider.last_prompt
    assert '"id":"core.datetime"' in provider.last_prompt
    detailed = provider.last_prompt.split(
        "Detailed candidate skills (the only selectable skills for this request): ",
        1,
    )[1].split("\nKnowledge excerpts:", 1)[0]
    assert '"id":"core.calculator"' in detailed
    assert '"id":"core.datetime"' not in detailed


@pytest.mark.asyncio
async def test_planner_uses_configured_timeout():
    provider = FakeProvider(
        {
            "disposition": "answer",
            "skill_id": "",
            "arguments": {},
            "answer": "Sure.",
            "confidence": 1,
            "clarification": None,
        }
    )
    planner = LocalPlanner(provider, SkillRetriever(()), timeout_seconds=45)

    await planner.plan("Can you help?")

    assert provider.last_timeout_seconds == 45


@pytest.mark.asyncio
async def test_planner_normalizes_launcher_source_argument():
    launcher = LauncherSkill(FakeApplications(), resolver=lambda name: "unused.exe")
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "windows.launcher",
            "arguments": {
                "operation": "open",
                "source": "youtube",
                "destination": None,
                "dry_run": False,
                "id": "",
            },
            "answer": "The skill 'windows.launcher' will be used to launch YouTube.",
            "confidence": 1,
            "clarification": None,
        }
    )
    planner = LocalPlanner(provider, SkillRetriever((launcher.manifest,)))

    intent = await planner.plan("open youtube")

    assert intent.name == "windows.launcher"
    assert intent.arguments == {"application": "youtube"}


@pytest.mark.asyncio
async def test_planner_can_explain_its_role_and_available_skills():
    provider = FakeProvider(
        {
            "disposition": "answer",
            "skill_id": "",
            "arguments": {},
            "answer": (
                "I am your local Windows assistant. I can calculate expressions and "
                "report the date and time using registered skills."
            ),
            "confidence": 0.99,
            "clarification": None,
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((CalculatorSkill.manifest, DateTimeSkill.manifest)),
    )

    intent = await planner.plan("What is your role and what can you do?")

    assert intent.name == "conversation.answer"
    assert "Skill directory:" in provider.last_prompt
    assert "questions about your role or capabilities" in provider.last_prompt


@pytest.mark.asyncio
async def test_planner_accepts_percentage_confidence_for_answer():
    provider = FakeProvider(
        {
            "disposition": "answer",
            "skill_id": "",
            "arguments": {},
            "answer": "Visit youtube.com to sign up.",
            "confidence": 100,
            "clarification": "This request asks how to register for YouTube.",
        }
    )
    planner = LocalPlanner(provider, SkillRetriever(()))

    intent = await planner.plan("register for youtube")

    assert intent.name == "conversation.answer"
    assert intent.arguments["text"] == "Visit youtube.com to sign up."
    assert intent.confidence == 1


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
async def test_planner_infers_missing_skill_id_from_valid_arguments():
    planner = LocalPlanner(
        FakeProvider(
            {
                "disposition": "skill",
                "skill_id": "",
                "arguments": {"seconds": 60, "label": "1 minute timer"},
                "answer": "",
                "confidence": 0.95,
                "clarification": "None",
            }
        ),
        SkillRetriever(
            (CalculatorSkill.manifest, DateTimeSkill.manifest, TimerSkill.manifest)
        ),
    )

    intent = await planner.plan("set a timer for one minute")

    assert intent.name == "core.timer"
    assert intent.arguments == {"seconds": 60, "label": "1 minute timer"}
    assert intent.ambiguous is False


@pytest.mark.asyncio
async def test_planner_uses_answer_when_skill_call_has_no_executable_arguments():
    planner = LocalPlanner(
        FakeProvider(
            {
                "disposition": "skill",
                "skill_id": "",
                "arguments": {},
                "answer": (
                    "I can set a timer for you. A 10-second timer will go off "
                    "in 10 seconds."
                ),
                "confidence": 1,
                "clarification": None,
            }
        ),
        SkillRetriever((CalculatorSkill.manifest,)),
    )

    intent = await planner.plan("set a timer for ten seconds")

    assert intent.name == "conversation.answer"
    assert "10-second timer" in intent.arguments["text"]


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
async def test_planner_repairs_invalid_structured_output_once():
    provider = FakeProvider(
        [
            {
                "disposition": "answer",
                "skill_id": "",
                "arguments": {},
                "answer": "",
                "confidence": 0.98,
                "clarification": None,
            },
            {
                "disposition": "answer",
                "skill_id": "",
                "arguments": {},
                "answer": "Paris is the capital of France.",
                "confidence": 0.98,
                "clarification": None,
            },
        ]
    )
    planner = LocalPlanner(provider, SkillRetriever((CalculatorSkill.manifest,)))

    intent = await planner.plan("What is the capital of France?")

    assert intent.name == "conversation.answer"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_planner_stops_after_one_invalid_output_retry():
    invalid = {
        "disposition": "answer",
        "skill_id": "",
        "arguments": {},
        "answer": "",
        "confidence": 0.98,
        "clarification": None,
    }
    provider = FakeProvider([invalid, invalid])
    planner = LocalPlanner(provider, SkillRetriever(()))

    with pytest.raises(InvalidPlannerResponse) as exc_info:
        await planner.plan("What is the capital of France?")

    assert provider.calls == 2
    assert exc_info.value.invalid_response == invalid


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


def test_retriever_does_not_match_skills_only_by_stop_words():
    retriever = SkillRetriever((CalculatorSkill.manifest, DateTimeSkill.manifest))
    assert retriever.relevant("What is the capital of France?") == ()


@pytest.mark.asyncio
async def test_planner_receives_relevant_knowledge_excerpts(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'knowledge.db'}")
    database.migrate()
    knowledge = KnowledgeBaseStore(database)
    knowledge.save("Project Atlas", "Atlas launches on October 14.")
    provider = FakeProvider(
        {
            "disposition": "answer",
            "skill_id": "",
            "arguments": {},
            "answer": "Atlas launches on October 14.",
            "confidence": 0.99,
            "clarification": None,
        }
    )
    planner = LocalPlanner(provider, SkillRetriever(()), knowledge)

    intent = await planner.plan("When does Atlas launch?")

    assert intent.arguments["text"] == "Atlas launches on October 14."
    assert "Atlas launches on October 14." in provider.last_prompt
