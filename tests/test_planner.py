import pytest
from jsonschema import ValidationError

from assistant.context.snapshot import ContextSnapshot
from assistant.language.planner import InvalidPlannerResponse, LocalPlanner, SkillRetriever
from assistant.language.retrieval import RetrievedContextItem
from assistant.language.understanding import UserIntentInterpretation
from assistant.skills.builtin import (
    CalculatorSkill,
    DateTimeSkill,
    FileSearchSkill,
    LauncherSkill,
    TimerSkill,
)
from assistant.skills.windows import ApplicationWindowSkill, FileOperationSkill
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


class FakeFileSystem:
    async def search(self, root, query, limit):
        return []


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
async def test_planner_normalizes_file_search_desktop_and_limit():
    search = FileSearchSkill(FakeFileSystem())
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "windows.file_search",
            "arguments": {
                "root": "/mnt/c/Users/<username>/Desktop",
                "query": "resume",
                "limit": 10000,
                "from_step": 1,
                "previous_result": None,
            },
            "answer": "",
            "confidence": 0.95,
            "clarification": "",
        }
    )
    planner = LocalPlanner(provider, SkillRetriever((search.manifest,)))

    intent = await planner.plan_decision("Find my resume on my desktop")

    assert intent.intent.name == "windows.file_search"
    assert intent.intent.arguments == {
        "root": "${user_desktop}",
        "query": "resume",
        "limit": 100,
    }


@pytest.mark.asyncio
async def test_planner_normalizes_window_process_name_punctuation():
    window = ApplicationWindowSkill(FakeApplications())
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "windows.application_window",
            "arguments": {
                "operation": "close",
                "process_name": "roblox.exe\ufe40",
            },
            "answer": "",
            "confidence": 1,
            "clarification": None,
        }
    )
    planner = LocalPlanner(provider, SkillRetriever((window.manifest,)))

    intent = await planner.plan("close roblox")

    assert intent.name == "windows.application_window"
    assert intent.arguments == {
        "operation": "close",
        "process_name": "RobloxPlayerBeta.exe",
    }


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
async def test_planner_accepts_empty_reject_answer_with_reason():
    planner = LocalPlanner(
        FakeProvider(
            {
                "disposition": "reject",
                "skill_id": "",
                "arguments": {
                    "reason": "The request cannot be handled by any registered skill."
                },
                "answer": "",
                "confidence": 0.95,
                "clarification": None,
            }
        ),
        SkillRetriever(()),
    )

    intent = await planner.plan("turn off my wifi")

    assert intent.name == "conversation.reject"
    assert intent.arguments["text"] == (
        "The request cannot be handled by any registered skill."
    )


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


@pytest.mark.asyncio
async def test_complex_planner_returns_schema_valid_steps():
    provider = FakeProvider(
        {
            "disposition": "plan",
            "skill_id": "",
            "arguments": {},
            "answer": None,
            "confidence": 0.9,
            "clarification": None,
            "steps": [
                {
                    "step_id": "math",
                    "skill_id": "core.calculator",
                    "arguments": {"expression": "2 + 2"},
                    "depends_on": [],
                },
                {
                    "step_id": "date",
                    "skill_id": "core.datetime",
                    "arguments": {"query": "date"},
                    "depends_on": [],
                },
            ],
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((CalculatorSkill.manifest, DateTimeSkill.manifest)),
    )

    decision = await planner.plan_decision("calculate 2 plus 2 and tell me the date")

    assert [step.step_id for step in decision.steps] == ["math", "date"]
    assert [step.intent.name for step in decision.steps] == [
        "core.calculator",
        "core.datetime",
    ]
    assert "You may return plan" in provider.last_prompt


@pytest.mark.asyncio
async def test_complex_planner_treats_single_action_plan_arguments_as_skill():
    provider = FakeProvider(
        {
            "disposition": "plan",
            "skill_id": "",
            "arguments": {
                "operation": "copy",
                "source": "${user_desktop}/AddTextFile.txt",
                "destination": "${assistant_folder}",
            },
            "answer": None,
            "confidence": 0.95,
            "clarification": None,
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((FileOperationSkill(object()).manifest,)),
    )

    decision = await planner.plan_decision(
        "Add a text file to the assistant folder on my desktop"
    )

    assert decision.intent.name == "windows.file_operation"
    assert decision.intent.arguments == {
        "operation": "copy",
        "source": "${user_desktop}/AddTextFile.txt",
        "destination": "${assistant_folder}",
    }
    assert decision.steps == ()


@pytest.mark.asyncio
async def test_plan_from_interpretation_uses_retrieved_candidate_skills():
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "windows.application_window",
            "arguments": {"operation": "minimize_focused"},
            "answer": None,
            "confidence": 0.92,
            "clarification": None,
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever(
            (
                CalculatorSkill.manifest,
                ApplicationWindowSkill(FakeApplications()).manifest,
            )
        ),
    )

    decision = await planner.plan_from_interpretation(
        "get this out of the way",
        interpretation=UserIntentInterpretation(
            user_goal="Minimize the focused application window",
            request_type="local_action",
            explicit_requests=["get this out of the way"],
            relevant_context_keys=["desktop.focused_application"],
            success_criteria=["focused window is minimized"],
            confidence=0.88,
        ),
        context=ContextSnapshot(request_text="get this out of the way", session_id="s1"),
        retrieved=(
            RetrievedContextItem(
                namespace="skill_example",
                source_id="windows.application_window:0",
                text="get this out of the way",
                metadata={"skill_id": "windows.application_window"},
                score=0.91,
            ),
        ),
    )

    assert decision.intent is not None
    assert decision.intent.name == "windows.application_window"
    assert "windows.application_window" in provider.last_prompt
    assert "core.calculator" not in provider.last_prompt


@pytest.mark.asyncio
async def test_plan_revision_rejects_unavailable_next_step_skill():
    provider = FakeProvider(
        {
            "disposition": "next_step",
            "rationale": "Use an unavailable capability.",
            "skill_id": "shell.run",
            "arguments": {"command": "echo hi"},
            "clarification": None,
            "response": None,
            "confidence": 0.8,
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((CalculatorSkill.manifest,)),
    )

    with pytest.raises(InvalidPlannerResponse):
        await planner.revise_plan(
            "calculate and run a shell command",
            interpretation=UserIntentInterpretation(
                user_goal="Calculate and run shell",
                request_type="multi_step_goal",
                explicit_requests=["calculate and run a shell command"],
                confidence=0.8,
            ),
            context=ContextSnapshot(
                request_text="calculate and run a shell command",
                session_id="s1",
            ),
            retrieved=(),
            observations=(
                {
                    "iteration": 1,
                    "skill_id": "core.calculator",
                    "outcome": "completed",
                    "summary": "4",
                },
            ),
        )


@pytest.mark.asyncio
async def test_complex_planner_treats_single_step_plan_as_skill():
    provider = FakeProvider(
        {
            "disposition": "plan",
            "skill_id": "",
            "arguments": {},
            "answer": None,
            "confidence": 0.9,
            "clarification": None,
            "steps": [
                {
                    "step_id": "math",
                    "skill_id": "core.calculator",
                    "arguments": {"expression": "2 + 2"},
                    "depends_on": [],
                },
            ],
        }
    )
    planner = LocalPlanner(provider, SkillRetriever((CalculatorSkill.manifest,)))

    decision = await planner.plan_decision("calculate 2 plus 2")

    assert decision.intent.name == "core.calculator"
    assert decision.intent.arguments == {"expression": "2 + 2"}
    assert decision.steps == ()


@pytest.mark.asyncio
async def test_complex_planner_can_request_clarification():
    provider = FakeProvider(
        {
            "disposition": "clarify",
            "skill_id": "",
            "arguments": {},
            "answer": None,
            "confidence": 0.8,
            "clarification": "Which folder should I search?",
            "steps": [],
        }
    )
    planner = LocalPlanner(provider, SkillRetriever((TimerSkill.manifest,)))

    decision = await planner.plan_decision("search for the report")

    assert decision.needs_clarification
    assert decision.clarification == "Which folder should I search?"


@pytest.mark.asyncio
async def test_complex_planner_uses_clarification_from_invalid_skill_call():
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "windows.file_operation",
            "arguments": {
                "operation": "clean up downloads",
                "destination": None,
            },
            "answer": (
                "I will help you clean up your Downloads folder by organizing "
                "the files."
            ),
            "confidence": 0.95,
            "clarification": (
                "Should I organize Downloads by file type, date, or project/name?"
            ),
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((FileOperationSkill(object()).manifest,)),
    )

    decision = await planner.plan_decision("Clean up my downloads")

    assert decision.intent is None
    assert decision.clarification == (
        "Should I organize Downloads by file type, date, or project/name?"
    )
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_interpreted_planner_uses_clarification_from_invalid_skill_call():
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "windows.file_operation",
            "arguments": {
                "operation": "clean up downloads",
                "destination": None,
            },
            "answer": None,
            "confidence": 0.95,
            "clarification": (
                "Should I organize Downloads by file type, date, or project/name?"
            ),
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((FileOperationSkill(object()).manifest,)),
    )

    decision = await planner.plan_from_interpretation(
        "Clean up my downloads",
        interpretation=UserIntentInterpretation(
            user_goal="Organize files in the Downloads folder",
            request_type="multi_step_goal",
            explicit_requests=["clean up my downloads"],
            confidence=0.86,
        ),
        context=ContextSnapshot(request_text="Clean up my downloads", session_id="s1"),
        retrieved=(
            RetrievedContextItem(
                namespace="skill_example",
                source_id="windows.file_operation:0",
                text="clean up my downloads",
                metadata={"skill_id": "windows.file_operation"},
                score=0.91,
            ),
        ),
    )

    assert decision.intent is None
    assert decision.clarification == (
        "Should I organize Downloads by file type, date, or project/name?"
    )
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_complex_planner_repairs_hallucinated_step_skill():
    provider = FakeProvider(
        [
            {
                "disposition": "plan",
                "skill_id": "",
                "arguments": {},
                "answer": None,
                "confidence": 1,
                "clarification": None,
                "steps": [
                    {
                        "step_id": "shell",
                        "skill_id": "shell.run",
                        "arguments": {"command": "format c:"},
                        "depends_on": [],
                    },
                    {
                        "step_id": "math",
                        "skill_id": "core.calculator",
                        "arguments": {"expression": "1 + 1"},
                        "depends_on": [],
                    },
                ],
            },
            {
                "disposition": "reject",
                "skill_id": "",
                "arguments": {},
                "answer": "I cannot run shell commands.",
                "confidence": 1,
                "clarification": None,
                "steps": [],
            },
        ]
    )
    planner = LocalPlanner(provider, SkillRetriever((CalculatorSkill.manifest,)))

    decision = await planner.plan_decision("run a shell command and calculate")

    assert decision.intent.name == "conversation.reject"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_complex_planner_rejects_step_output_placeholders():
    provider = FakeProvider(
        [
            {
                "disposition": "plan",
                "skill_id": "",
                "arguments": {},
                "answer": None,
                "confidence": 0.9,
                "clarification": None,
                "steps": [
                    {
                        "step_id": "search",
                        "skill_id": "web.search",
                        "arguments": {"query": "project atlas", "count": 5},
                        "depends_on": [],
                    },
                    {
                        "step_id": "fetch",
                        "skill_id": "web.fetch",
                        "arguments": {
                            "url": "<from step search>",
                            "max_characters": 12000,
                        },
                        "depends_on": ["search"],
                    },
                ],
            },
            {
                "disposition": "clarify",
                "skill_id": "",
                "arguments": {},
                "answer": None,
                "confidence": 0.8,
                "clarification": "Which search result should I open?",
                "steps": [],
            },
        ]
    )
    from assistant.skills.network import WebFetchSkill, WebSearchSkill

    planner = LocalPlanner(
        provider,
        SkillRetriever(
            (
                WebSearchSkill(None, lambda _name: None).manifest,
                WebFetchSkill(None).manifest,
            )
        ),
    )

    decision = await planner.plan_decision("search for Project Atlas and open it")

    assert decision.clarification == "Which search result should I open?"
    assert provider.calls == 2
