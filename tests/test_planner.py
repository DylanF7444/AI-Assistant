import pytest
from pydantic import create_model

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
    manifest,
)
from assistant.skills.windows import (
    ApplicationWindowSkill,
    FileOperationSkill,
    WindowsDeviceControlSkill,
)
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
        output = self.output[self.calls] if isinstance(self.output, list) else self.output
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
    assert "planning and conversational intelligence for Local Assistant" in (provider.last_prompt)
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
async def test_planner_retries_instead_of_promising_to_use_an_available_skill():
    provider = FakeProvider(
        [
            {
                "disposition": "answer",
                "skill_id": "",
                "arguments": {},
                "answer": (
                    "I will use the 'Windows Device Control' skill to read your "
                    "CPU and memory usage information."
                ),
                "confidence": 0.95,
                "clarification": None,
            },
            {
                "disposition": "skill",
                "skill_id": "windows.device_control",
                "arguments": {"operation": "performance_status"},
                "answer": None,
                "confidence": 0.98,
                "clarification": None,
            },
        ]
    )
    device = WindowsDeviceControlSkill(object())
    planner = LocalPlanner(provider, SkillRetriever((device.manifest,)))

    decision = await planner.plan_decision("What is my CPU and memory usage?")

    assert provider.calls == 2
    assert decision.intent is not None
    assert decision.intent.name == "windows.device_control"
    assert decision.intent.arguments == {"operation": "performance_status"}
    assert "deferred an available skill call" in provider.last_prompt


@pytest.mark.asyncio
async def test_planner_can_answer_a_question_about_using_an_available_skill():
    provider = FakeProvider(
        {
            "disposition": "answer",
            "skill_id": "",
            "arguments": {},
            "answer": "I can use the Calculator skill for arithmetic.",
            "confidence": 0.98,
            "clarification": None,
        }
    )
    planner = LocalPlanner(provider, SkillRetriever((CalculatorSkill.manifest,)))

    decision = await planner.plan_decision("Can you use the calculator skill?")

    assert provider.calls == 1
    assert decision.intent is not None
    assert decision.intent.name == "conversation.answer"


@pytest.mark.asyncio
async def test_contextual_follow_up_retrieves_skills_from_the_previous_turn():
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "windows.device_control",
            "arguments": {"operation": "performance_status"},
            "answer": None,
            "confidence": 0.98,
            "clarification": None,
        }
    )
    device = WindowsDeviceControlSkill(object())
    planner = LocalPlanner(provider, SkillRetriever((device.manifest,)))
    recent_context = (
        {
            "request": "What's my CPU and memory usage?",
            "intent": "conversation.answer",
            "outcome": "answered",
            "response_type": "answer",
            "severity": "info",
            "response": "I can read that with Windows Device Control.",
        },
    )

    decision = await planner.plan_decision("go ahead", recent_context)

    assert decision.intent is not None
    assert decision.intent.name == "windows.device_control"
    assert decision.intent.arguments == {"operation": "performance_status"}
    detailed = provider.last_prompt.split(
        "Detailed candidate skills (the only selectable skills for this request): ",
        1,
    )[1].split("\nKnowledge excerpts:", 1)[0]
    assert '"id":"windows.device_control"' in detailed


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
async def test_planner_normalizes_launcher_executable_to_application_alias():
    launcher = LauncherSkill(FakeApplications(), resolver=lambda name: "unused.exe")
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "windows.launcher",
            "arguments": {"application": "notepad.exe"},
            "answer": "Opened Notepad.",
            "confidence": 1,
            "clarification": None,
        }
    )
    planner = LocalPlanner(provider, SkillRetriever((launcher.manifest,)))

    intent = await planner.plan("open a blank notepad")

    assert intent.name == "windows.launcher"
    assert intent.arguments == {"application": "notepad"}


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
async def test_planner_retries_when_schema_valid_skill_does_not_match_goal():
    window = ApplicationWindowSkill(FakeApplications())
    files = FileOperationSkill(FakeFileSystem())
    provider = FakeProvider(
        [
            {
                "disposition": "skill",
                "skill_id": "windows.application_window",
                "arguments": {
                    "operation": "move_to_monitor",
                    "process_name": "assistant.exe",
                    "monitor": 2,
                },
                "answer": None,
                "confidence": 0.99,
                "clarification": None,
            },
            {
                "disposition": "skill",
                "skill_id": "windows.file_operation",
                "arguments": {
                    "operation": "organize",
                    "source": "${user_desktop}/assistant",
                    "categories": {
                        "Images": "images",
                        "Text Files": "text_files",
                    },
                },
                "answer": None,
                "confidence": 0.96,
                "clarification": None,
            },
        ]
    )
    planner = LocalPlanner(provider, SkillRetriever((window.manifest, files.manifest)))

    decision = await planner.plan_decision(
        "Separate the files in the assistant folder on my desktop into two "
        "folders, one with images and one with text files."
    )

    assert provider.calls == 2
    assert decision.intent is not None
    assert decision.intent.name == "windows.file_operation"
    assert decision.intent.arguments["operation"] == "organize"
    assert decision.intent.arguments["categories"] == {
        "Images": "images",
        "Text Files": "text_files",
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
                "arguments": {"reason": "The request cannot be handled by any registered skill."},
                "answer": "",
                "confidence": 0.95,
                "clarification": None,
            }
        ),
        SkillRetriever(()),
    )

    intent = await planner.plan("turn off my wifi")

    assert intent.name == "conversation.reject"
    assert intent.arguments["text"] == ("The request cannot be handled by any registered skill.")


@pytest.mark.asyncio
async def test_planner_discards_stray_skill_id_from_action_free_reject():
    planner = LocalPlanner(
        FakeProvider(
            {
                "disposition": "reject",
                "skill_id": "windows.application_window",
                "arguments": {},
                "answer": "I cannot help with that request.",
                "confidence": 0.95,
                "clarification": None,
            }
        ),
        SkillRetriever((ApplicationWindowSkill(FakeApplications()).manifest,)),
    )

    intent = await planner.plan("Explain an unsupported request")

    assert intent.name == "conversation.reject"
    assert intent.arguments["text"] == "I cannot help with that request."


@pytest.mark.asyncio
async def test_open_ended_file_organization_request_uses_model_interpretation():
    question = "Which file types should go into each destination folder?"
    provider = FakeProvider(
        {
            "user_goal": "Organize document files into two user-defined categories",
            "request_type": "multi_step_goal",
            "explicit_requests": ["separate files into Roblox and macro folders"],
            "missing_information": ["file-type mapping for each folder"],
            "should_ask_clarification": True,
            "clarification_question": question,
            "confidence": 0.94,
        }
    )
    planner = LocalPlanner(provider, SkillRetriever(()))
    request = (
        "Seperate the files in my documents into two different folders, one with "
        "roblox place files and one with macro files"
    )

    interpretation = await planner.interpret(
        request,
        context=ContextSnapshot(request_text=request, session_id="s1"),
        retrieved=(),
    )

    assert interpretation.should_ask_clarification is True
    assert interpretation.clarification_question == question
    assert provider.calls == 1


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
        SkillRetriever((CalculatorSkill.manifest, DateTimeSkill.manifest, TimerSkill.manifest)),
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
                    "I can set a timer for you. A 10-second timer will go off in 10 seconds."
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
async def test_planner_repairs_promise_to_move_files_instead_of_requesting_confirmation():
    file_operation = FileOperationSkill(FakeFileSystem())
    invalid_promise = {
        "disposition": "answer",
        "skill_id": "",
        "arguments": {
            "operation": "move",
            "source": "${documents_folder}/roblox_places/*.*",
            "destination": "${documents_folder}/roblox_places",
        },
        "answer": (
            "I will help you move the Roblox places files into a new folder. "
            "After that, I'll create another folder for your macro files and move "
            "them there. Please confirm if this is what you want to do."
        ),
        "confidence": 0.95,
        "clarification": None,
    }
    repaired = {
        "disposition": "plan",
        "skill_id": "windows.file_operation",
        "arguments": invalid_promise["arguments"],
        "answer": invalid_promise["answer"],
        "confidence": 0.9,
        "clarification": (
            "Please provide the names of the files that should be moved to the "
            "'macro_files' folder."
        ),
    }
    provider = FakeProvider([invalid_promise, repaired])
    planner = LocalPlanner(provider, SkillRetriever((file_operation.manifest,)))

    decision = await planner.plan_decision(
        "Separate .rbxl and .rbxlx files in my documents into Roblox Places, and "
        ".ahk and .mcr files into Macro Files"
    )

    assert provider.calls == 2
    assert decision.clarification == repaired["clarification"]
    assert decision.intent is None
    assert decision.steps == ()
    assert "cannot include a skill or action arguments" in provider.last_prompt


@pytest.mark.asyncio
async def test_planner_repairs_invented_macro_into_registered_file_organization_skill():
    file_operation = FileOperationSkill(FakeFileSystem())
    provider = FakeProvider(
        [
            {
                "disposition": "skill",
                "skill_id": "core.macro",
                "arguments": {"name": "SeparateFilesIntoFolders"},
                "answer": None,
                "confidence": 0.9,
                "clarification": None,
            },
            {
                "disposition": "skill",
                "skill_id": "windows.file_operation",
                "arguments": {
                    "operation": "organize",
                    "source": "documents",
                    "rules": {
                        "Roblox Places": [".rbxl", ".rbxlx"],
                        "Macro Files": [".ahk"],
                    },
                },
                "answer": None,
                "confidence": 0.95,
                "clarification": None,
            },
        ]
    )

    def validate_arguments(skill_id, arguments):
        if skill_id == "core.macro":
            raise ValueError("Unknown configured macro: SeparateFilesIntoFolders")
        assert skill_id == "windows.file_operation"
        file_operation.validate(arguments)

    planner = LocalPlanner(
        provider,
        SkillRetriever(
            (
                manifest(
                    skill_id="core.macro",
                    display_name="Macro",
                    description="Run an existing configured macro by exact saved name.",
                    input_model=create_model("ConfiguredMacroInput", name=(str, ...)),
                    output_model=create_model("ConfiguredMacroOutput", name=(str, ...)),
                ),
                file_operation.manifest,
            )
        ),
        argument_validator=validate_arguments,
    )

    decision = await planner.plan_decision("Roblox Places: .rbxl and .rbxlx; Macro Files: .ahk")

    assert provider.calls == 2
    assert decision.intent is not None
    assert decision.intent.name == "windows.file_operation"
    assert decision.intent.arguments["operation"] == "organize"
    assert "Unknown configured macro" in provider.last_prompt


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

    decision = await planner.plan_decision("Add a text file to the assistant folder on my desktop")

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
    assert "core.calculator" in provider.last_prompt


@pytest.mark.asyncio
async def test_interpreted_planner_merges_retrieved_and_lexical_skill_candidates():
    launcher = LauncherSkill(FakeApplications(), resolver=lambda name: "unused.exe")
    window = ApplicationWindowSkill(FakeApplications())
    provider = FakeProvider(
        {
            "disposition": "skill",
            "skill_id": "windows.launcher",
            "arguments": {"application": "notepad"},
            "answer": None,
            "confidence": 0.96,
            "clarification": None,
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((launcher.manifest, window.manifest)),
    )

    decision = await planner.plan_from_interpretation(
        "I need a blank Notepad window",
        interpretation=UserIntentInterpretation(
            user_goal="Open a new blank Notepad application instance",
            request_type="local_action",
            explicit_requests=["I need a blank Notepad window"],
            success_criteria=["a new blank Notepad window is open"],
            confidence=0.94,
        ),
        context=ContextSnapshot(
            request_text="I need a blank Notepad window",
            session_id="s1",
        ),
        retrieved=(
            RetrievedContextItem(
                namespace="skill",
                source_id="windows.application_window",
                text="Change an existing application window",
                metadata={"skill_id": "windows.application_window"},
                score=0.91,
            ),
        ),
    )

    assert decision.intent is not None
    assert decision.intent.name == "windows.launcher"
    assert decision.intent.arguments == {"application": "notepad"}
    assert '"id":"windows.application_window"' in provider.last_prompt
    assert '"id":"windows.launcher"' in provider.last_prompt


@pytest.mark.asyncio
async def test_planner_repairs_arguments_rejected_by_real_skill_validation():
    launcher = LauncherSkill(FakeApplications(), resolver=lambda name: "unused.exe")
    window = ApplicationWindowSkill(FakeApplications())
    skills = {
        launcher.manifest.skill_id: launcher,
        window.manifest.skill_id: window,
    }
    provider = FakeProvider(
        [
            {
                "disposition": "skill",
                "skill_id": "windows.application_window",
                "arguments": {"operation": "focus"},
                "answer": None,
                "confidence": 0.8,
                "clarification": None,
            },
            {
                "disposition": "skill",
                "skill_id": "windows.launcher",
                "arguments": {"application": "notepad"},
                "answer": None,
                "confidence": 0.96,
                "clarification": None,
            },
        ]
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever(tuple(skill.manifest for skill in skills.values())),
        argument_validator=lambda skill_id, arguments: skills[skill_id].validate(arguments),
    )

    decision = await planner.plan_decision("I need a blank Notepad window")

    assert provider.calls == 2
    assert decision.intent is not None
    assert decision.intent.name == "windows.launcher"
    assert decision.intent.arguments == {"application": "notepad"}
    assert "focus requires process_name" in provider.last_prompt


@pytest.mark.asyncio
async def test_interpreted_planner_rejects_existing_window_action_for_new_instance_goal():
    launcher = LauncherSkill(FakeApplications(), resolver=lambda name: "unused.exe")
    window = ApplicationWindowSkill(FakeApplications())
    wrong_focus = {
        "disposition": "skill",
        "skill_id": "windows.application_window",
        "arguments": {"operation": "focus", "process_name": "notepad.exe"},
        "answer": "Focused Notepad.",
        "confidence": 0.98,
        "clarification": "",
        "steps": [
            {
                "step_id": "1",
                "skill_id": "windows.application_window",
                "arguments": {"operation": "focus", "process_name": "notepad.exe"},
                "depends_on": [],
                "timeout_seconds": 60,
            }
        ],
    }
    provider = FakeProvider([wrong_focus, wrong_focus])
    planner = LocalPlanner(
        provider,
        SkillRetriever((launcher.manifest, window.manifest)),
    )

    decision = await planner.plan_from_interpretation(
        "I need a blank notepad",
        interpretation=UserIntentInterpretation(
            user_goal="I need a blank notepad",
            request_type="unsupported",
            confidence=0.87,
        ),
        context=ContextSnapshot(request_text="I need a blank notepad", session_id="s1"),
        retrieved=(
            RetrievedContextItem(
                namespace="skill",
                source_id="windows.application_window",
                text="Focus an existing visible process window",
                metadata={"skill_id": "windows.application_window"},
                score=0.9,
            ),
        ),
    )

    assert provider.calls == 2
    assert decision.intent is not None
    assert decision.intent.name == "windows.launcher"
    assert decision.intent.arguments == {"application": "notepad"}
    assert "start a new application instance" in provider.last_prompt


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

    revision = await planner.revise_plan(
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

    assert revision.disposition == "clarify"
    assert "registered skill" in revision.clarification


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
@pytest.mark.parametrize("internal_label", ["needs_clarification", "operation"])
async def test_complex_planner_replaces_internal_clarification_label_with_real_question(
    internal_label,
):
    provider = FakeProvider(
        {
            "disposition": "clarify",
            "skill_id": "",
            "arguments": {"operation": "move_files_by_type"},
            "answer": (
                "Great! To clarify further, can you please specify which file types "
                "belong in the Roblox Places folder and which belong in Macro Files? "
                "Would you like to proceed now?"
            ),
            "confidence": 0.95,
            "clarification": internal_label,
            "steps": [],
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((FileOperationSkill(object()).manifest,)),
    )

    decision = await planner.plan_decision("Move the files based on file type, not name.")

    assert decision.clarification == (
        "To clarify further, can you please specify which file types belong in the "
        "Roblox Places folder and which belong in Macro Files?"
    )
    assert decision.clarification not in {"needs_clarification", "operation"}


@pytest.mark.asyncio
async def test_action_with_internal_clarification_label_uses_question_from_answer():
    provider = FakeProvider(
        {
            "disposition": "plan",
            "skill_id": "windows.file_search",
            "arguments": {"root": "documents", "query": "roblox"},
            "answer": (
                "Do you have specific preferences for the folder structure? "
                "Would you like to proceed now?"
            ),
            "confidence": 0.95,
            "clarification": "needs_clarification",
            "steps": [
                {
                    "step_id": "search",
                    "skill_id": "windows.file_search",
                    "arguments": {"root": "documents", "query": "roblox"},
                    "depends_on": [],
                }
            ],
        }
    )
    planner = LocalPlanner(
        provider,
        SkillRetriever((FileSearchSkill(FakeFileSystem()).manifest,)),
    )

    decision = await planner.plan_decision("Organize my Roblox files.")

    assert decision.intent is None
    assert decision.steps == ()
    assert decision.clarification == ("Do you have specific preferences for the folder structure?")


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
            "answer": ("I will help you clean up your Downloads folder by organizing the files."),
            "confidence": 0.95,
            "clarification": ("Should I organize Downloads by file type, date, or project/name?"),
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
            "clarification": ("Should I organize Downloads by file type, date, or project/name?"),
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
