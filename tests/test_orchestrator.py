import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import ValidationError

from assistant.bootstrap import build_container
from assistant.contracts import (
    Intent,
    PipelineEvent,
    PipelineState,
    ResponseType,
    ResultStatus,
    Transcript,
    WordTimestamp,
)
from assistant.language.planner import (
    InvalidPlannerResponse,
    PlannedIntentStep,
    PlannerDecision,
    PlanRevision,
)
from assistant.language.retrieval import RetrievedContextItem
from assistant.language.understanding import UserIntentInterpretation
from assistant.policy.permissions import GrantType
from assistant.storage.database import HistoryRow
from assistant.windows.adapters import WindowsApplicationAdapter


class FakeTTS:
    def __init__(self):
        self.spoken = []
        self.cancelled = False

    async def speak(self, response):
        self.spoken.append(response.speech_text)
        yield PipelineEvent(
            correlation_id=uuid4(),
            state=PipelineState.SPEAKING,
            message="speaking",
        )

    async def health(self):
        return {"healthy": True}

    async def cancel(self):
        self.cancelled = True


class GatedSTT:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def transcribe(self, session, audio):
        self.started.set()
        async for _chunk in audio:
            yield Transcript(
                session_id=session.session_id,
                raw_text="calculate 1 plus 1",
                normalized_text="calculate 1 plus 1",
                confidence=0.95,
            )
            return

    async def health(self):
        return {"healthy": True}

    async def cancel(self):
        self.cancelled = True


class PartialThenFinalSTT:
    async def transcribe(self, session, audio):
        async for _chunk in audio:
            yield Transcript(
                session_id=session.session_id,
                raw_text="uh",
                normalized_text="uh",
                confidence=0.4,
                is_final=False,
            )
            yield Transcript(
                session_id=session.session_id,
                raw_text="calculate 1 plus 1",
                normalized_text="calculate 1 plus 1",
                confidence=0.95,
                is_final=True,
            )
            return

    async def health(self):
        return {"healthy": True}

    async def cancel(self):
        pass


class BrokenPlanner:
    async def plan(self, text, recent_context=()):
        raise ConnectionError("connection refused")

    async def cancel(self):
        pass


class SlowPlanner:
    async def plan(self, text, recent_context=()):
        raise TimeoutError("planner timed out")

    async def cancel(self):
        pass


class AnsweringPlanner:
    async def plan(self, text, recent_context=()):
        return Intent(
            name="conversation.answer",
            arguments={"text": "A direct answer."},
            confidence=0.9,
            source="llm",
        )

    async def cancel(self):
        pass


class InvalidPlanner:
    async def plan(self, text, recent_context=()):
        raise ValidationError("invalid structured response")

    async def cancel(self):
        pass


class InvalidPlannerWithResponse:
    async def plan(self, text, recent_context=()):
        raise InvalidPlannerResponse(
            "'confidence' is a required property",
            {
                "disposition": "answer",
                "skill_id": "",
                "arguments": {},
                "answer": "I can help with that.",
            },
        )

    async def cancel(self):
        pass


class CapturingPlanner:
    def __init__(self):
        self.calls = []

    async def plan(self, text, recent_context=()):
        self.calls.append((text, recent_context))
        return Intent(
            name="conversation.answer",
            arguments={"text": "Use Settings to add an application alias."},
            confidence=0.9,
            source="llm",
        )

    async def cancel(self):
        pass


class ComplexPlanner:
    async def plan_decision(self, text, recent_context=()):
        return PlannerDecision(
            steps=(
                PlannedIntentStep(
                    step_id="math",
                    intent=Intent(
                        name="core.calculator",
                        arguments={"expression": "2 + 2"},
                        confidence=0.95,
                        source="llm",
                    ),
                ),
                PlannedIntentStep(
                    step_id="time",
                    intent=Intent(
                        name="core.datetime",
                        arguments={"query": "date"},
                        confidence=0.95,
                        source="llm",
                    ),
                    depends_on=("math",),
                ),
            )
        )

    async def cancel(self):
        pass


class ClarifyingDecisionPlanner:
    async def plan_decision(self, text, recent_context=()):
        return PlannerDecision(clarification="Which folder should I search?")

    async def cancel(self):
        pass


class ClarifyThenAnswerPlanner:
    def __init__(self):
        self.calls = []

    async def plan_decision(self, text, recent_context=()):
        self.calls.append((text, recent_context))
        if len(self.calls) == 1:
            return PlannerDecision(clarification="Which folder should I search?")
        return PlannerDecision(
            intent=Intent(
                name="conversation.answer",
                arguments={"text": "Resolved follow-up."},
                confidence=0.9,
                source="llm",
            )
        )

    async def cancel(self):
        pass


class InterpretingClarifyingPlanner:
    def __init__(self):
        self.calls = []

    async def interpret(self, text, *, context, retrieved):
        self.calls.append((text, context, retrieved))
        return UserIntentInterpretation(
            user_goal="Organize files in the Downloads folder",
            request_type="multi_step_goal",
            explicit_requests=["clean up my downloads"],
            implicit_needs=["inspect folder contents", "avoid deleting files"],
            missing_information=["preferred organization method"],
            constraints=["preview before moving files"],
            risk_notes=["moving files changes filesystem state"],
            success_criteria=["files are grouped predictably"],
            should_ask_clarification=True,
            clarification_question=(
                "Should I organize Downloads by file type, date, or project/name?"
            ),
            confidence=0.86,
        )

    async def plan_decision(self, text, recent_context=()):
        raise AssertionError("clarifying interpretation should stop before planning")

    async def cancel(self):
        pass


class InterpretingThenAnswerPlanner:
    def __init__(self):
        self.interpret_calls = []
        self.plan_calls = []

    async def interpret(self, text, *, context, retrieved):
        self.interpret_calls.append((text, context, retrieved))
        return UserIntentInterpretation(
            user_goal="Understand the broad request",
            request_type="question",
            explicit_requests=[text],
            should_ask_clarification=False,
            confidence=0.8,
        )

    async def plan_from_interpretation(self, text, *, interpretation, context, retrieved):
        self.plan_calls.append((text, interpretation, context, retrieved))
        return PlannerDecision(
            intent=Intent(
                name="conversation.answer",
                arguments={"text": "Handled by the interpreted planner path."},
                confidence=0.9,
                source="llm",
            )
        )

    async def plan_decision(self, text, recent_context=()):
        raise AssertionError("broad goals should use plan_from_interpretation")

    async def cancel(self):
        pass


class ReplanningPlanner:
    def __init__(self, revisions):
        self.revisions = list(revisions)
        self.interpret_calls = []
        self.plan_calls = []
        self.revision_calls = []

    async def interpret(self, text, *, context, retrieved):
        self.interpret_calls.append((text, context, retrieved))
        return UserIntentInterpretation(
            user_goal="Run a multi-step calculation workflow",
            request_type="multi_step_goal",
            explicit_requests=[text],
            should_ask_clarification=False,
            confidence=0.9,
        )

    async def plan_from_interpretation(self, text, *, interpretation, context, retrieved):
        self.plan_calls.append((text, interpretation, context, retrieved))
        return PlannerDecision(
            intent=Intent(
                name="core.calculator",
                arguments={"expression": "2 + 2"},
                confidence=0.95,
                source="llm",
            )
        )

    async def revise_plan(self, text, *, interpretation, context, retrieved, observations):
        self.revision_calls.append((text, observations))
        if self.revisions:
            return self.revisions.pop(0)
        return PlanRevision(
            disposition="stop",
            rationale="Done.",
            response="Workflow complete.",
            confidence=0.9,
        )

    async def cancel(self):
        pass


class InterpretingLaunchPlanner:
    async def interpret(self, text, *, context, retrieved):
        return UserIntentInterpretation(
            user_goal="Open a new blank Notepad application instance",
            request_type="local_action",
            explicit_requests=[text],
            success_criteria=["a new blank Notepad window is open"],
            confidence=0.95,
        )

    async def plan_from_interpretation(self, text, *, interpretation, context, retrieved):
        return PlannerDecision(
            intent=Intent(
                name="windows.launcher",
                arguments={"application": "notepad"},
                confidence=0.97,
                source="llm",
            )
        )

    async def revise_plan(self, text, *, interpretation, context, retrieved, observations):
        return PlanRevision(
            disposition="stop",
            rationale="The requested application instance was launched.",
            response="Opened Notepad.",
            confidence=0.98,
        )

    async def cancel(self):
        pass


class FileOrganizationPlanner:
    def __init__(self):
        self.interpret_calls = []
        self.plan_calls = []

    async def interpret(self, text, *, context, retrieved):
        self.interpret_calls.append(text)
        has_mapping = ".rbxl" in text and ".rbxlx" in text and ".ahk" in text
        return UserIntentInterpretation(
            user_goal="Organize documents into extension-based subfolders",
            request_type="multi_step_goal",
            explicit_requests=[text],
            missing_information=[] if has_mapping else ["extension mapping"],
            should_ask_clarification=not has_mapping,
            clarification_question=(
                None if has_mapping else "Which file extensions belong in each destination folder?"
            ),
            confidence=0.95,
        )

    async def plan_from_interpretation(
        self,
        text,
        *,
        interpretation,
        context,
        retrieved,
    ):
        self.plan_calls.append(text)
        return PlannerDecision(
            intent=Intent(
                name="windows.file_operation",
                arguments={
                    "operation": "organize",
                    "source": "documents",
                    "rules": {
                        "Roblox Places": [".rbxl", ".rbxlx"],
                        "Macro Files": [".ahk"],
                    },
                },
                confidence=0.96,
                source="llm",
            )
        )

    async def cancel(self):
        pass


class FakeSemanticRetriever:
    def __init__(self):
        self.calls = []

    async def search(self, query, *, namespaces, limit=8):
        self.calls.append((query, namespaces, limit))
        return (
            RetrievedContextItem(
                namespace="skill_example",
                source_id="windows.file_operation:0",
                text="clean up my downloads",
                metadata={"skill_id": "windows.file_operation"},
                score=0.91,
            ),
        )


class FakeSkillIndexer:
    def __init__(self):
        self.calls = 0

    async def refresh(self, registry):
        self.calls += 1


class FakeMemoryIndexer:
    def __init__(self):
        self.calls = 0

    async def refresh(self, memory_store):
        self.calls += 1


@pytest.mark.asyncio
async def test_text_to_command_to_response(settings):
    container = build_container(settings)
    response, outcome = await container.orchestrator.handle_text("calculate 4 times 5")
    assert outcome.result.status == ResultStatus.COMPLETED
    assert response.response_type == ResponseType.SKILL
    assert "20" in response.display_text
    assert "Request breakdown:" not in response.display_text


@pytest.mark.asyncio
async def test_daily_message_creates_persistent_schedule_without_using_planner(settings):
    container = build_container(settings)
    container.orchestrator.planner = BrokenPlanner()

    response, outcome = await container.orchestrator.handle_text(
        "Say good morning! everyday at 8 AM"
    )

    assert outcome.result is not None
    assert outcome.result.status == ResultStatus.COMPLETED
    assert outcome.call is not None
    assert outcome.call.skill_id == "core.schedule"
    assert "Created schedule good morning!" in response.display_text
    schedules = container.schedule_store.list()
    assert len(schedules) == 1
    assert schedules[0].status.value == "active"
    assert schedules[0].trigger.frequency == "daily"
    assert schedules[0].trigger.local_time.isoformat() == "08:00:00"
    assert schedules[0].action.kind == "notify_only"
    assert schedules[0].action.message == "good morning!"
    assert container.notification_center.history() == ()


@pytest.mark.asyncio
async def test_naturally_worded_launch_goal_executes_selected_skill(settings, tmp_path):
    settings.confirmation_overrides = {"skill:windows.launcher": False}
    container = build_container(settings)
    target = tmp_path / "notepad.exe"
    target.write_bytes(b"")
    container.aliases.set("application", "notepad", str(target))
    launched = []

    async def record_launch(path):
        launched.append(path)

    container.registry.get("windows.launcher").adapter.launch = record_launch
    container.permissions.grant(
        user_id="local-user",
        skill_id="windows.launcher",
        capability="process.launch",
        grant_type=GrantType.ALWAYS,
        targets=(str(target.resolve()),),
    )
    container.orchestrator.planner = InterpretingLaunchPlanner()

    response, outcome = await container.orchestrator.handle_text("I need a blank Notepad window")

    assert outcome.result is not None
    assert outcome.result.status == ResultStatus.COMPLETED
    assert outcome.call is not None
    assert outcome.call.skill_id == "windows.launcher"
    assert outcome.call.arguments == {"application": "notepad"}
    assert launched == [str(target)]
    assert response.severity == "success"


@pytest.mark.asyncio
async def test_undo_command_reports_when_last_command_cannot_be_undone(settings):
    container = build_container(settings)
    await container.orchestrator.handle_text("calculate 4 times 5")

    response, outcome = await container.orchestrator.handle_text("undo")

    assert outcome.result.status == ResultStatus.DENIED
    assert outcome.result.error.code == "undo_not_supported"
    assert response.response_type == ResponseType.CONTROL
    assert response.severity == "warning"
    assert "cannot be undone" in response.display_text


@pytest.mark.asyncio
async def test_reasoning_steps_can_be_shown(settings):
    settings.show_reasoning_steps = True
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text("calculate 4 times 5")

    assert outcome.result.status == ResultStatus.COMPLETED
    assert "20" in response.display_text
    assert "Request breakdown:" in response.display_text
    assert "core.calculator" in response.display_text
    assert "Prepare core.calculator" in response.display_text
    assert "Request breakdown:" not in (response.speech_text or "")


@pytest.mark.asyncio
async def test_terminal_debug_is_printed_for_text_requests(settings, capsys):
    container = build_container(settings)

    await container.orchestrator.handle_text("calculate 4 times 5")

    captured = capsys.readouterr()
    assert "[assistant debug]" in captured.err
    assert "request=calculate 4 times 5" in captured.err
    assert "intent=core.calculator" in captured.err
    assert 'arguments={"expression": "4 * 5"}' in captured.err
    assert "result_status=completed" in captured.err


@pytest.mark.asyncio
async def test_terminal_debug_can_be_disabled(settings, capsys):
    settings.terminal_debug = False
    container = build_container(settings)

    await container.orchestrator.handle_text("calculate 4 times 5")

    captured = capsys.readouterr()
    assert "[assistant debug]" not in captured.err


def test_terminal_debug_setting_is_applied_to_ai_provider(settings):
    container = build_container(settings)
    provider = container.orchestrator.planner.provider

    assert provider.terminal_debug is True

    container.orchestrator.terminal_debug = False

    assert provider.terminal_debug is False


@pytest.mark.asyncio
async def test_sensitive_action_is_denied_without_permission(settings):
    container = build_container(settings)
    response, outcome = await container.orchestrator.handle_text("play music")
    assert outcome.result.status == ResultStatus.DENIED
    assert outcome.permission_requests
    assert "media.control" in response.display_text
    assert "Grant Permission" in response.display_text


@pytest.mark.asyncio
async def test_file_search_select_and_contextual_copy_preview(settings, tmp_path):
    container = build_container(settings)
    documents = tmp_path / "documents"
    destination_folder = tmp_path / "desktop"
    documents.mkdir()
    destination_folder.mkdir()
    source = documents / "resume.txt"
    source.write_text("resume")
    destination = destination_folder / source.name
    container.permissions.grant(
        user_id="local-user",
        skill_id="windows.file_search",
        capability="filesystem.read",
        grant_type=GrantType.ALWAYS,
        targets=(str(documents.resolve()),),
    )

    response, outcome = await container.orchestrator.handle_text(f"find resume in {documents}")
    assert outcome.result.status == ResultStatus.COMPLETED
    assert "1." in response.display_text
    assert str(source.resolve()) in response.display_text

    response, _ = await container.orchestrator.handle_text("select 1")
    assert str(source.resolve()) in response.display_text

    response, outcome = await container.orchestrator.handle_text(f"copy it to {destination_folder}")
    assert response.response_type == ResponseType.PERMISSION
    assert len(outcome.permission_requests) == 2
    assert [request.scope.capability for request in outcome.permission_requests] == [
        "filesystem.read",
        "filesystem.write",
    ]
    assert outcome.permission_requests[0].scope.targets == (str(source.resolve()),)
    assert outcome.permission_requests[1].scope.targets == (str(destination.resolve()),)

    for request in outcome.permission_requests:
        container.permissions.grant(
            user_id=request.call.permission_context.user_id,
            skill_id=request.call.skill_id,
            capability=request.scope.capability,
            grant_type=GrantType.ALWAYS,
            targets=request.scope.targets,
            correlation_id=str(request.call.correlation_id),
        )
    continued = await container.engine.prepare(outcome.call)
    assert continued.confirmation is not None
    assert continued.confirmation.exact_action == (
        f"Copy {source.resolve()} to {destination.resolve()}"
    )


@pytest.mark.asyncio
async def test_file_search_result_ordinal_copy_uses_named_documents_folder(settings, tmp_path):
    container = build_container(settings)
    search_root = tmp_path / "search"
    search_root.mkdir()
    source = search_root / "resume.txt"
    source.write_text("resume")
    expected_destination = Path.home() / "Documents" / source.name
    container.permissions.grant(
        user_id="local-user",
        skill_id="windows.file_search",
        capability="filesystem.read",
        grant_type=GrantType.ALWAYS,
        targets=(str(search_root.resolve()),),
    )

    response, outcome = await container.orchestrator.handle_text(f"find resume in {search_root}")
    assert outcome.result.status == ResultStatus.COMPLETED
    assert str(source.resolve()) in response.display_text

    response, outcome = await container.orchestrator.handle_text(
        "Copy and paste the first result into my documents folder"
    )

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call.skill_id == "windows.file_operation"
    assert outcome.call.arguments["source"] == str(source.resolve())
    assert (
        outcome.call.arguments["destination"].casefold()
        == str(expected_destination.resolve()).casefold()
    )
    assert outcome.permission_requests[0].scope.targets == (str(source.resolve()),)
    assert (
        outcome.permission_requests[1].scope.targets[0].casefold()
        == str(expected_destination.resolve()).casefold()
    )


@pytest.mark.asyncio
async def test_file_search_result_ordinal_open_uses_matching_candidate(settings, tmp_path):
    container = build_container(settings)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one")
    second.write_text("two")
    container.orchestrator.remember_file_candidates([str(first.resolve()), str(second.resolve())])

    response, outcome = await container.orchestrator.handle_text("Open the second one")

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call.skill_id == "windows.file_operation"
    assert outcome.call.arguments == {
        "operation": "open",
        "source": str(second.resolve()),
    }
    assert outcome.permission_requests[0].scope.targets == (str(second.resolve()),)


@pytest.mark.asyncio
async def test_file_search_named_delete_prefers_exact_candidate_stem(settings, tmp_path):
    container = build_container(settings)
    desktop = tmp_path / "Desktop"
    archive = desktop / "CodeArchive" / "Assistant" / ".venv"
    starspace = desktop / "PROJECTS" / "Starspace" / "node_modules"
    archive.mkdir(parents=True)
    starspace.mkdir(parents=True)
    resume = desktop / "Resume.txt"
    resume.write_text("resume")
    noisy_python = archive / "resume_execution.py"
    noisy_python.write_text("code")
    noisy_js = starspace / "GeneratorResume.js"
    noisy_js.write_text("code")
    container.orchestrator.remember_file_candidates(
        [
            str(resume.resolve()),
            str(noisy_python.resolve()),
            str(noisy_js.resolve()),
        ]
    )

    response, outcome = await container.orchestrator.handle_text("Delete the resume in my desktop")

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call.skill_id == "windows.file_operation"
    assert outcome.call.arguments == {
        "operation": "delete",
        "source": str(resume.resolve()),
    }
    assert len(outcome.permission_requests) == 1
    assert outcome.permission_requests[0].scope.targets == (str(resume.resolve()),)


@pytest.mark.asyncio
async def test_unresolved_file_operation_source_asks_for_exact_file(settings):
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text("Delete the resume in my desktop")

    assert response.response_type == ResponseType.CLARIFICATION
    assert "could not resolve" in response.display_text
    assert "select 1" in response.display_text
    assert "System32" not in response.display_text
    assert outcome.call is None


@pytest.mark.asyncio
async def test_create_text_file_in_assistant_folder_requests_exact_write_permission(settings):
    container = build_container(settings)
    desktop = settings.data_dir.parent / "Desktop"
    target_folder = desktop / "PROJECTS" / "Assistant"
    target_folder.mkdir(parents=True)
    target = target_folder / "my_text_file.txt"
    container.permissions.grant(
        user_id="local-user",
        skill_id="windows.file_search",
        capability="filesystem.read",
        grant_type=GrantType.ALWAYS,
        targets=(str(desktop.resolve()),),
    )

    response, outcome = await container.orchestrator.handle_text(
        "Add a text file to the assistant folder in my desktop"
    )

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call.skill_id == "windows.file_operation"
    assert outcome.call.arguments["operation"] == "create_text"
    assert len(outcome.permission_requests) == 1
    request = outcome.permission_requests[0]
    assert request.scope.capability == "filesystem.write"
    assert request.scope.targets == (str(target.resolve()),)


@pytest.mark.asyncio
async def test_create_text_file_in_assistant_folder_asks_where_to_search(settings):
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text(
        "Add a text file to the assistant folder in my desktop"
    )

    assert response.response_type == ResponseType.CLARIFICATION
    assert "allowed to search" in response.display_text
    assert "Reply with the folder" in response.display_text
    assert "next message as the follow-up" in response.display_text
    assert outcome.result is None


@pytest.mark.asyncio
async def test_folder_search_clarification_follow_up_resumes_original_request(settings):
    container = build_container(settings)
    search_root = settings.data_dir / "FollowUpSearchRoot"
    target_folder = search_root / "PROJECTS" / "Assistant"
    target_folder.mkdir(parents=True)
    target = target_folder / "my_text_file.txt"

    first, first_outcome = await container.orchestrator.handle_text(
        "Add a text file to the assistant folder in my desktop"
    )
    assert first.response_type == ResponseType.CLARIFICATION
    assert first_outcome.result is None

    response, outcome = await container.orchestrator.handle_text(str(search_root))

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call.skill_id == "windows.file_operation"
    assert outcome.call.arguments["operation"] == "create_text"
    assert outcome.call.arguments["source"] == str(target.resolve())
    assert len(outcome.permission_requests) == 1
    request = outcome.permission_requests[0]
    assert request.scope.capability == "filesystem.write"
    assert request.scope.targets == (str(target.resolve()),)
    grants = container.permissions.list_grants()
    assert any(
        grant["skill_id"] == "windows.file_search"
        and grant["capability"] == "filesystem.read"
        and grant["grant_type"] == GrantType.SESSION.value
        and grant["targets"] == [str(search_root.resolve())]
        for grant in grants
    )


@pytest.mark.asyncio
async def test_router_clarification_follow_up_completes_previous_conversion_request(settings):
    container = build_container(settings)

    first, first_outcome = await container.orchestrator.handle_text(
        "convert a few miles to kilometers"
    )
    assert first.response_type == ResponseType.CLARIFICATION
    assert first_outcome.result is None

    response, outcome = await container.orchestrator.handle_text("5")

    assert response.response_type == ResponseType.SKILL
    assert outcome.call.skill_id == "core.unit_conversion"
    assert outcome.call.arguments == {
        "value": 5,
        "from_unit": "miles",
        "to_unit": "kilometers",
    }
    assert outcome.result.status == ResultStatus.COMPLETED


@pytest.mark.asyncio
async def test_new_request_clears_pending_clarification(settings):
    container = build_container(settings)

    first, _ = await container.orchestrator.handle_text("convert a few miles to kilometers")
    assert first.response_type == ResponseType.CLARIFICATION

    response, outcome = await container.orchestrator.handle_text("new request calculate 2 plus 3")

    assert response.response_type == ResponseType.SKILL
    assert outcome.call.skill_id == "core.calculator"
    assert outcome.result.status == ResultStatus.COMPLETED
    assert "5" in response.display_text


@pytest.mark.asyncio
async def test_cancel_clears_pending_clarification(settings):
    container = build_container(settings)

    first, _ = await container.orchestrator.handle_text("convert a few miles to kilometers")
    assert first.response_type == ResponseType.CLARIFICATION

    response, outcome = await container.orchestrator.handle_text("cancel")

    assert response.response_type == ResponseType.CONTROL
    assert "Cancelled" in response.display_text
    assert outcome.result is None


@pytest.mark.asyncio
async def test_selected_file_delete_requires_exact_write_confirmation(settings, tmp_path):
    container = build_container(settings)
    source = tmp_path / "resume.txt"
    source.write_text("resume")
    container.orchestrator.remember_file_candidates([str(source.resolve())])
    await container.orchestrator.handle_text("select 1")

    response, outcome = await container.orchestrator.handle_text("delete it")
    assert response.response_type == ResponseType.PERMISSION
    assert len(outcome.permission_requests) == 1
    request = outcome.permission_requests[0]
    assert request.scope.capability == "filesystem.write"
    assert request.scope.targets == (str(source.resolve()),)
    container.permissions.grant(
        user_id=request.call.permission_context.user_id,
        skill_id=request.call.skill_id,
        capability=request.scope.capability,
        grant_type=GrantType.ALWAYS,
        targets=request.scope.targets,
        correlation_id=str(request.call.correlation_id),
    )

    continued = await container.engine.prepare(outcome.call)
    assert continued.confirmation is not None
    assert continued.confirmation.exact_action == f"Delete {source.resolve()}"
    assert response.response_type == ResponseType.PERMISSION
    assert response.severity == "warning"


@pytest.mark.asyncio
async def test_text_confirm_executes_the_exact_pending_file_action(settings, tmp_path):
    container = build_container(settings)
    source = tmp_path / "resume.txt"
    source.write_text("resume")
    destination_folder = tmp_path / "archive"
    destination_folder.mkdir()
    destination = destination_folder / source.name
    container.orchestrator.remember_file_candidates([str(source.resolve())])
    await container.orchestrator.handle_text("select 1")
    for capability, targets in (
        ("filesystem.read", (str(source.resolve()),)),
        ("filesystem.write", (str(destination.resolve()),)),
    ):
        container.permissions.grant(
            user_id="local-user",
            skill_id="windows.file_operation",
            capability=capability,
            grant_type=GrantType.ALWAYS,
            targets=targets,
        )

    preview, pending = await container.orchestrator.handle_text(f"copy it to {destination_folder}")
    assert preview.response_type == ResponseType.CONFIRMATION
    assert pending.confirmation is not None

    response, outcome = await container.orchestrator.handle_text("Confirm")

    assert response.response_type == ResponseType.CONTROL
    assert outcome.result is not None
    assert outcome.result.status == ResultStatus.COMPLETED
    assert destination.read_text() == "resume"


@pytest.mark.asyncio
async def test_router_converts_spoken_temperature_with_degrees_word(settings):
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text(
        "Convert 68 degrees Fahrenheit to Celsius"
    )

    assert response.response_type == ResponseType.SKILL
    assert outcome.call.skill_id == "core.unit_conversion"
    assert outcome.call.arguments == {
        "value": 68,
        "from_unit": "fahrenheit",
        "to_unit": "celsius",
    }
    assert outcome.result.status == ResultStatus.COMPLETED
    assert outcome.result.data["value"] == pytest.approx(20)


@pytest.mark.asyncio
async def test_text_confirm_without_pending_action_is_not_routed_as_a_denied_skill(settings):
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text("Confirm")

    assert response.response_type == ResponseType.CONTROL
    assert response.display_text == "There is no pending action to confirm."
    assert outcome.call is None
    assert outcome.result is None


@pytest.mark.asyncio
async def test_voice_only_tts_speaks_voice_invocations_once(settings):
    container = build_container(settings)
    tts = FakeTTS()
    container.orchestrator.tts = tts
    container.orchestrator.speech_mode = "voice_only"
    await container.orchestrator.handle_text("what time")
    assert tts.spoken == []
    transcript = Transcript(
        session_id=uuid4(),
        raw_text="what time",
        normalized_text="what time",
    )
    await container.orchestrator.handle_transcript(transcript)
    assert len(tts.spoken) == 1
    await container.orchestrator.cancel()
    assert tts.cancelled


@pytest.mark.asyncio
async def test_persistent_voice_ignores_sound_until_explicit_stop(settings):
    container = build_container(settings)
    request = asyncio.create_task(container.orchestrator.handle_text("set a timer for 10 seconds"))

    for _ in range(100):
        jobs = container.engine.active_jobs()
        if jobs:
            break
        await asyncio.sleep(0.01)
    assert jobs
    request_correlation = jobs[0].correlation_id
    assert container.orchestrator.request_in_progress

    # A concurrent persistent listener uses its own correlation ID. It must not
    # replace the request that a later explicit stop should cancel.
    container.orchestrator._active_correlation = uuid4()
    assert await container.orchestrator.consume_persistent_voice_interrupt("background clatter")
    await asyncio.sleep(0)
    assert not request.done()
    assert container.engine.active_jobs()[0].correlation_id == request_correlation

    assert await container.orchestrator.consume_persistent_voice_interrupt("stop")
    _response, outcome = await asyncio.wait_for(request, timeout=1)

    assert outcome.result is not None
    assert outcome.result.status == ResultStatus.CANCELLED
    assert not container.orchestrator.request_in_progress


@pytest.mark.asyncio
async def test_listen_once_stays_listening_until_transcript_arrives(settings):
    container = build_container(settings)
    stt = GatedSTT()
    release_audio = asyncio.Event()
    events = []
    container.events.add_listener(events.append)

    async def audio():
        await release_audio.wait()
        yield b"\x01\x00" * 400

    task = asyncio.create_task(container.orchestrator.listen_once(stt, audio()))
    await asyncio.wait_for(stt.started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert [event.state for event in events] == [PipelineState.LISTENING]

    release_audio.set()
    response, outcome = await task

    assert "2" in response.display_text
    assert outcome.result is not None
    assert PipelineState.TRANSCRIBING in [event.state for event in events]


@pytest.mark.asyncio
async def test_passive_transcribe_once_can_stay_quiet_until_request(settings):
    container = build_container(settings)
    stt = GatedSTT()
    release_audio = asyncio.Event()
    events = []
    transcripts = []
    container.events.add_listener(events.append)

    async def audio():
        await release_audio.wait()
        yield b"\x01\x00" * 400

    task = asyncio.create_task(
        container.orchestrator.transcribe_once(
            stt,
            audio(),
            publish_events=False,
            on_transcript=transcripts.append,
        )
    )
    await asyncio.wait_for(stt.started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert events == []

    release_audio.set()
    transcript = await task

    assert transcript is not None
    assert transcript.normalized_text == "calculate 1 plus 1"
    assert events == []
    assert [item.raw_text for item in transcripts] == ["calculate 1 plus 1"]


@pytest.mark.asyncio
async def test_listen_once_routes_final_transcript_not_partial_filler(settings):
    container = build_container(settings)

    async def audio():
        yield b"\x01\x00" * 400

    response, outcome = await container.orchestrator.listen_once(PartialThenFinalSTT(), audio())

    assert "2" in response.display_text
    assert outcome.result is not None


@pytest.mark.asyncio
async def test_empty_voice_transcript_does_not_update_the_display(settings):
    container = build_container(settings)
    transcript = Transcript(
        session_id=uuid4(),
        raw_text="",
        normalized_text="",
        confidence=None,
    )

    response, outcome = await container.orchestrator.handle_transcript(transcript)

    assert response.display_text == "No speech was detected."
    assert response.response_type == ResponseType.STATUS
    assert response.speech_text is None
    assert response.updates_display is False
    assert outcome.result is None


@pytest.mark.asyncio
async def test_session_context_is_bounded_correctable_and_forgettable(settings):
    container = build_container(settings)
    await container.orchestrator.handle_text("calculate 1 plus 1")
    await container.orchestrator.handle_text("calculate 2 plus 2")
    assert len(container.orchestrator._session_context) == 2
    corrected, _ = await container.orchestrator.handle_text("correct that to calculate 3 plus 4")
    assert "7" in corrected.display_text
    assert len(container.orchestrator._session_context) == 2
    await container.orchestrator.handle_text("forget this session")
    assert len(container.orchestrator._session_context) == 0


@pytest.mark.asyncio
async def test_low_confidence_sensitive_target_requires_clarification(settings):
    container = build_container(settings)
    transcript = Transcript(
        session_id=uuid4(),
        raw_text="delete file c colon users report",
        normalized_text="delete file c:/users/report.txt",
        confidence=0.6,
        words=(
            WordTimestamp(
                word="report",
                start_seconds=1,
                end_seconds=1.4,
                confidence=0.4,
            ),
        ),
    )
    response, outcome = await container.orchestrator.handle_transcript(transcript)
    assert "not confident" in response.display_text
    assert response.response_type == ResponseType.CLARIFICATION


@pytest.mark.asyncio
async def test_low_confidence_sensitive_target_prints_voice_debug(settings, capsys):
    container = build_container(settings)
    transcript = Transcript(
        session_id=uuid4(),
        raw_text="delete file c colon users report",
        normalized_text="delete file c:/users/report.txt",
        confidence=0.7,
        words=(
            WordTimestamp(
                word="report",
                start_seconds=1,
                end_seconds=1.4,
                confidence=0.42,
            ),
        ),
    )

    response, outcome = await container.orchestrator.handle_transcript(transcript)

    captured = capsys.readouterr()
    assert "[assistant voice debug]" in captured.err
    assert "gate=sensitive_target_confidence" in captured.err
    assert "transcript_confidence=0.7" in captured.err
    assert "transcript_threshold=0.75" in captured.err
    assert '"word": "report"' in captured.err
    assert "intent=windows.file_operation" in captured.err
    assert '"source"' in captured.err
    assert "not confident" in response.display_text
    assert outcome.result is None


@pytest.mark.asyncio
async def test_unavailable_planner_keeps_deterministic_commands_available(settings):
    container = build_container(settings)
    container.orchestrator.planner = BrokenPlanner()
    response, outcome = await container.orchestrator.handle_text("do something unmatched")
    assert response.severity == "warning"
    assert "Deterministic commands still work" in response.display_text
    assert outcome.result is None


@pytest.mark.asyncio
async def test_location_first_named_file_search_does_not_use_local_planner(settings):
    container = build_container(settings)
    container.orchestrator.planner = BrokenPlanner()

    response, outcome = await container.orchestrator.handle_text(
        "Look in my desktop for a file named resume"
    )

    assert response.response_type == ResponseType.PERMISSION
    assert "local planner" not in response.display_text
    assert len(outcome.permission_requests) == 1
    request = outcome.permission_requests[0]
    assert request.call.skill_id == "windows.file_search"
    assert request.call.arguments == {
        "query": "resume",
        "root": "desktop",
        "limit": 20,
    }
    assert request.scope.capability == "filesystem.read"


@pytest.mark.asyncio
async def test_switch_window_to_monitor_does_not_use_local_planner(settings):
    container = build_container(settings)
    container.orchestrator.planner = BrokenPlanner()

    response, outcome = await container.orchestrator.handle_text(
        "Switch notepad to my first monitor"
    )

    assert response.response_type == ResponseType.PERMISSION
    assert "local planner" not in response.display_text
    assert len(outcome.permission_requests) == 1
    assert outcome.permission_requests[0].call.skill_id == "windows.application_window"
    assert outcome.permission_requests[0].call.arguments == {
        "operation": "move_to_monitor",
        "process_name": "notepad.exe",
        "monitor": 1,
    }


@pytest.mark.asyncio
async def test_turn_off_my_wifi_does_not_use_local_planner(settings):
    container = build_container(settings)
    container.orchestrator.planner = BrokenPlanner()

    response, outcome = await container.orchestrator.handle_text("Turn off my wifi")

    assert response.response_type == ResponseType.PERMISSION
    assert "local planner" not in response.display_text
    assert len(outcome.permission_requests) == 1
    request = outcome.permission_requests[0]
    assert request.call.skill_id == "windows.device_control"
    assert request.call.arguments == {
        "operation": "toggle_wifi",
        "enabled": False,
    }
    assert request.scope.capability == "system.network"


@pytest.mark.asyncio
async def test_cpu_and_memory_question_uses_device_skill_without_local_planner(settings):
    container = build_container(settings)
    container.orchestrator.planner = BrokenPlanner()

    response, outcome = await container.orchestrator.handle_text("Whats my cpu and memory usage?")

    assert response.response_type == ResponseType.PERMISSION
    assert "local planner" not in response.display_text
    assert len(outcome.permission_requests) == 1
    request = outcome.permission_requests[0]
    assert request.call.skill_id == "windows.device_control"
    assert request.call.arguments == {"operation": "performance_status"}
    assert request.scope.capability == "device.read"
    assert container.orchestrator._session_context[-1]["intent"] == ("windows.device_control")


@pytest.mark.asyncio
async def test_set_my_device_volume_does_not_use_local_planner(settings):
    container = build_container(settings)
    container.orchestrator.planner = BrokenPlanner()

    response, outcome = await container.orchestrator.handle_text("Set my device volume to 80")

    assert response.response_type == ResponseType.PERMISSION
    assert "local planner" not in response.display_text
    assert len(outcome.permission_requests) == 1
    request = outcome.permission_requests[0]
    assert request.call.skill_id == "windows.device_control"
    assert request.call.arguments == {
        "operation": "set_volume",
        "level": 80,
    }
    assert request.scope.capability == "media.control"


@pytest.mark.asyncio
async def test_unmute_my_audio_does_not_use_local_planner(settings):
    container = build_container(settings)
    container.orchestrator.planner = BrokenPlanner()

    response, outcome = await container.orchestrator.handle_text("Unmute my audio")

    assert response.response_type == ResponseType.PERMISSION
    assert "local planner" not in response.display_text
    assert len(outcome.permission_requests) == 1
    request = outcome.permission_requests[0]
    assert request.call.skill_id == "windows.device_control"
    assert request.call.arguments == {"operation": "unmute_audio"}
    assert request.scope.capability == "media.control"


@pytest.mark.asyncio
async def test_invalid_planner_output_does_not_claim_ollama_is_stopped(settings):
    container = build_container(settings)
    container.orchestrator.planner = InvalidPlanner()

    response, outcome = await container.orchestrator.handle_text("do something unmatched")

    assert "invalid planner response" in response.display_text
    assert "Start Ollama" not in response.display_text
    assert outcome.result is None


@pytest.mark.asyncio
async def test_invalid_planner_output_includes_debug_response(settings):
    container = build_container(settings)
    container.orchestrator.planner = InvalidPlannerWithResponse()

    response, outcome = await container.orchestrator.handle_text("do something unmatched")

    assert "invalid planner response" in response.display_text
    assert "Debug invalid planner response" in response.display_text
    assert '"answer": "I can help with that."' in response.display_text
    assert "Validation error: 'confidence' is a required property" in response.display_text
    assert outcome.result is None


@pytest.mark.asyncio
async def test_planner_timeout_does_not_claim_ollama_is_stopped(settings):
    container = build_container(settings)
    container.orchestrator.planner = SlowPlanner()

    response, outcome = await container.orchestrator.handle_text("do something unmatched")

    assert "did not respond before the planner timeout" in response.display_text
    assert "Start Ollama" not in response.display_text
    assert outcome.result is None


@pytest.mark.asyncio
async def test_conversational_answer_does_not_execute_a_skill(settings):
    container = build_container(settings)
    container.orchestrator.planner = AnsweringPlanner()
    response, outcome = await container.orchestrator.handle_text("Tell me something")
    assert response.display_text == "A direct answer."
    assert response.response_type == ResponseType.ANSWER
    assert outcome.result is None


@pytest.mark.asyncio
async def test_explicit_memory_command_is_stored_and_listed(settings):
    container = build_container(settings)

    remembered, remember_outcome = await container.orchestrator.handle_text(
        "Remember that I prefer Brave for browsing."
    )
    listed, list_outcome = await container.orchestrator.handle_text(
        "What do you remember about me?"
    )

    assert remembered.response_type == ResponseType.SKILL
    assert remember_outcome.result.data["memories"][0]["key"] == ("i_prefer_brave_for_browsing")
    assert listed.response_type == ResponseType.SKILL
    assert "1 enabled memories" in listed.display_text
    assert list_outcome.result.data["memories"][0]["value"] == ("I prefer Brave for browsing")


@pytest.mark.asyncio
async def test_complex_planner_decision_executes_prepared_plan(settings):
    container = build_container(settings)
    container.orchestrator.planner = ComplexPlanner()

    response, outcome = await container.orchestrator.handle_text(
        "do a couple unmatched local things"
    )

    assert outcome.result.status == ResultStatus.COMPLETED
    assert response.response_type == ResponseType.SKILL
    assert "math: The result is 4." in response.display_text
    assert "time:" in response.display_text
    assert container.orchestrator._session_context[-1]["intent"] == "planner.plan"


@pytest.mark.asyncio
async def test_complex_planner_decision_can_ask_clarification(settings):
    container = build_container(settings)
    container.orchestrator.planner = ClarifyingDecisionPlanner()

    response, outcome = await container.orchestrator.handle_text(
        "do something unmatched with files"
    )

    assert response.response_type == ResponseType.CLARIFICATION
    assert response.display_text == "Which folder should I search?"
    assert outcome.result is None


@pytest.mark.asyncio
async def test_broad_goal_interpretation_can_ask_clarification(settings):
    container = build_container(settings)
    planner = InterpretingClarifyingPlanner()
    container.orchestrator.planner = planner

    response, outcome = await container.orchestrator.handle_text("Clean up my downloads.")

    assert response.response_type == ResponseType.CLARIFICATION
    assert response.display_text == (
        "Should I organize Downloads by file type, date, or project/name?"
    )
    assert outcome.result is None
    assert len(planner.calls) == 1
    assert planner.calls[0][1].request_text == "Clean up my downloads."
    assert {
        "skills.available",
        "skills.risk_summary",
        "skills.permission_summary",
        "permissions.granted",
        "permissions.file_policy",
        "permissions.confirmation_required",
    }.issubset(set(planner.calls[0][1].keys()))
    assert any(
        item.namespace == "skill_example" and item.metadata["skill_id"] == "windows.file_operation"
        for item in planner.calls[0][2]
    )


@pytest.mark.asyncio
async def test_open_ended_file_organization_follow_up_plans_registered_skill(settings):
    container = build_container(settings)
    planner = FileOrganizationPlanner()
    container.orchestrator.planner = planner

    first, first_outcome = await container.orchestrator.handle_text(
        "Separate the files in my documents into Roblox Places and Macro Files folders"
    )
    response, outcome = await container.orchestrator.handle_text(
        "Roblox Places: .rbxl and .rbxlx; Macro Files: .ahk"
    )

    assert first.response_type == ResponseType.CLARIFICATION
    assert first_outcome.result is None
    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call is not None
    assert outcome.call.skill_id == "windows.file_operation"
    assert outcome.call.arguments["operation"] == "organize"
    assert outcome.call.arguments["rules"] == {
        "Roblox Places": [".rbxl", ".rbxlx"],
        "Macro Files": [".ahk"],
    }
    assert len(planner.interpret_calls) == 2
    assert len(planner.plan_calls) == 1


@pytest.mark.asyncio
async def test_broad_goal_interpretation_plans_from_interpretation(settings):
    container = build_container(settings)
    planner = InterpretingThenAnswerPlanner()
    container.orchestrator.planner = planner

    response, outcome = await container.orchestrator.handle_text("Help me think through this.")

    assert response.response_type == ResponseType.ANSWER
    assert response.display_text == "Handled by the interpreted planner path."
    assert outcome.result is None
    assert len(planner.interpret_calls) == 1
    assert len(planner.plan_calls) == 1


@pytest.mark.asyncio
async def test_broad_goal_interpretation_receives_semantic_retrieval(settings):
    container = build_container(settings)
    planner = InterpretingClarifyingPlanner()
    retriever = FakeSemanticRetriever()
    indexer = FakeSkillIndexer()
    memory_indexer = FakeMemoryIndexer()
    container.orchestrator.planner = planner
    container.orchestrator.semantic_retriever = retriever
    container.orchestrator.skill_semantic_indexer = indexer
    container.orchestrator.memory_semantic_indexer = memory_indexer

    response, outcome = await container.orchestrator.handle_text("Clean up my downloads.")

    assert response.response_type == ResponseType.CLARIFICATION
    assert outcome.result is None
    assert indexer.calls == 1
    assert memory_indexer.calls == 1
    assert retriever.calls == [
        (
            "Clean up my downloads.",
            {
                "knowledge",
                "memory",
                "playbook",
                "skill",
                "skill_example",
                "workflow_template",
            },
            8,
        )
    ]
    assert planner.calls[0][2][0].namespace == "skill_example"
    assert planner.calls[0][2][0].metadata["skill_id"] == "windows.file_operation"


@pytest.mark.asyncio
async def test_explicit_playbook_use_injects_exact_enabled_playbook(settings):
    container = build_container(settings)
    planner = InterpretingThenAnswerPlanner()
    container.orchestrator.planner = planner
    container.playbook_store.save_markdown(
        container.playbook_store.EXAMPLE.replace(
            "prepare-release",
            "release-check",
        ).replace(
            "Prepare a release",
            "Release Check",
        )
    )

    response, outcome = await container.orchestrator.handle_text(
        "Use playbook Release Check to prepare version 2.0"
    )

    assert response.response_type == ResponseType.ANSWER
    assert outcome.result is None
    retrieved = planner.interpret_calls[0][2]
    assert retrieved[0].namespace == "playbook"
    assert retrieved[0].source_id == "release-check"
    assert retrieved[0].score == 1.0


@pytest.mark.asyncio
async def test_explicit_playbook_use_reports_unknown_or_disabled_reference(settings):
    container = build_container(settings)
    planner = InterpretingThenAnswerPlanner()
    container.orchestrator.planner = planner
    saved = container.playbook_store.save_markdown(container.playbook_store.EXAMPLE)
    container.playbook_store.set_enabled(saved.playbook_id, False)

    disabled_response, _ = await container.orchestrator.handle_text("Use playbook prepare-release")
    unknown_response, _ = await container.orchestrator.handle_text("Use playbook missing-workflow")
    enabled_response, _ = await container.orchestrator.handle_text(
        "enable playbook prepare-release"
    )

    assert disabled_response.response_type == ResponseType.ANSWER
    assert "is disabled" in disabled_response.display_text
    assert unknown_response.response_type == ResponseType.ANSWER
    assert "could not find a playbook" in unknown_response.display_text
    assert "Enabled playbook" in enabled_response.display_text
    assert container.playbook_store.get("prepare-release").enabled is True
    assert planner.interpret_calls == []


@pytest.mark.asyncio
async def test_broad_goal_interpretation_receives_memory_context_and_retrieval(settings):
    container = build_container(settings)
    planner = InterpretingThenAnswerPlanner()
    container.orchestrator.planner = planner
    container.memory_store.remember(
        type="preference",
        key="browser",
        value="User prefers Brave for browsing.",
        source="user",
        confidence=1,
    )

    response, outcome = await container.orchestrator.handle_text("Use my usual browser.")

    assert response.response_type == ResponseType.ANSWER
    assert outcome.result is None
    context = planner.interpret_calls[0][1]
    retrieved = planner.interpret_calls[0][2]
    assert "memory.preferences" in context.items_by_key()
    assert context.items_by_key()["memory.preferences"].value[0]["key"] == "browser"
    assert any(
        item.namespace == "memory" and item.metadata["key"] == "browser" and "Brave" in item.text
        for item in retrieved
    )


@pytest.mark.asyncio
async def test_broad_goal_replanning_can_continue_after_observation(settings):
    container = build_container(settings)
    planner = ReplanningPlanner(
        [
            PlanRevision(
                disposition="next_step",
                rationale="Need the date after the calculation.",
                skill_id="core.datetime",
                arguments={"query": "date"},
                confidence=0.9,
            ),
            PlanRevision(
                disposition="stop",
                rationale="Both steps completed.",
                response="Workflow complete.",
                confidence=0.95,
            ),
        ]
    )
    container.orchestrator.planner = planner

    response, outcome = await container.orchestrator.handle_text(
        "Help me calculate and then check the date."
    )

    assert response.response_type == ResponseType.STATUS
    assert response.display_text == "Workflow complete."
    assert outcome.result is not None
    assert len(planner.revision_calls) == 2
    assert planner.revision_calls[0][1][0]["skill_id"] == "core.calculator"
    assert planner.revision_calls[1][1][1]["skill_id"] == "core.datetime"


@pytest.mark.asyncio
async def test_broad_goal_replanning_can_stop_for_clarification(settings):
    container = build_container(settings)
    planner = ReplanningPlanner(
        [
            PlanRevision(
                disposition="clarify",
                rationale="Need user choice.",
                clarification="Which result should I use?",
                confidence=0.8,
            )
        ]
    )
    container.orchestrator.planner = planner

    response, outcome = await container.orchestrator.handle_text(
        "Help me calculate and choose the next thing."
    )

    assert response.response_type == ResponseType.CLARIFICATION
    assert response.display_text == "Which result should I use?"
    assert outcome.result is not None
    assert len(planner.revision_calls) == 1


@pytest.mark.asyncio
async def test_broad_goal_replanning_stops_at_loop_limit(settings):
    container = build_container(settings)
    planner = ReplanningPlanner(
        [
            PlanRevision(
                disposition="next_step",
                rationale="Try another calculation.",
                skill_id="core.calculator",
                arguments={"expression": "2 + 2"},
                confidence=0.9,
            )
            for _ in range(10)
        ]
    )
    container.orchestrator.planner = planner

    response, outcome = await container.orchestrator.handle_text("Help me keep calculating.")

    assert response.response_type == ResponseType.ERROR
    assert "replanning limit" in response.display_text
    assert outcome.result is not None
    assert len(planner.revision_calls) == 5


@pytest.mark.asyncio
async def test_request_breakdown_includes_interpretation_without_retrieved_text(settings):
    settings = settings.model_copy(update={"show_reasoning_steps": True})
    container = build_container(settings)
    planner = InterpretingThenAnswerPlanner()
    container.orchestrator.planner = planner

    class SensitiveRetriever:
        async def search(self, query, *, namespaces, limit=8):
            return (
                RetrievedContextItem(
                    namespace="memory",
                    source_id="1",
                    text="secret token should not appear",
                    metadata={"type": "preference", "key": "browser"},
                    score=0.75,
                ),
            )

    container.orchestrator.semantic_retriever = SensitiveRetriever()

    response, outcome = await container.orchestrator.handle_text("Use my usual browser.")

    assert response.response_type == ResponseType.ANSWER
    assert outcome.result is None
    assert "Request breakdown:" in response.display_text
    assert "- User goal: Understand the broad request" in response.display_text
    assert "- Context keys:" in response.display_text
    assert '"namespace": "memory"' in response.display_text
    assert "secret token should not appear" not in response.display_text


@pytest.mark.asyncio
async def test_planner_clarification_follow_up_includes_previous_request(settings):
    container = build_container(settings)
    planner = ClarifyThenAnswerPlanner()
    container.orchestrator.planner = planner

    first, first_outcome = await container.orchestrator.handle_text(
        "do something unmatched with files"
    )
    assert first.response_type == ResponseType.CLARIFICATION
    assert first_outcome.result is None

    response, outcome = await container.orchestrator.handle_text("Desktop")

    assert response.response_type == ResponseType.ANSWER
    assert response.display_text == "Resolved follow-up."
    assert outcome.result is None
    assert len(planner.calls) == 2
    follow_up_text = planner.calls[1][0]
    assert "do something unmatched with files" in follow_up_text
    assert "Which folder should I search?" in follow_up_text
    assert "Desktop" in follow_up_text


@pytest.mark.asyncio
async def test_file_search_limit_follow_up_completes_original_desktop_search(settings):
    container = build_container(settings)
    container.orchestrator._set_pending_clarification(
        original_text="Find my resume on my desktop",
        clarification="Please specify the limit for search results, which should be between 1 and 100.",
        session_id="desktop",
    )

    response, outcome = await container.orchestrator.handle_text("100")

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call.skill_id == "windows.file_search"
    assert outcome.call.arguments == {
        "query": "resume",
        "root": "desktop",
        "limit": 100,
    }


@pytest.mark.asyncio
async def test_explicit_skill_follow_up_completes_pending_file_search(settings):
    container = build_container(settings)
    container.orchestrator._set_pending_clarification(
        original_text="Find my resume in the assistant folder on my desktop",
        clarification=(
            "I could not map the next step to a registered skill. "
            "What exact outcome and targets should I use to build the plan?"
        ),
        session_id="desktop",
    )

    response, outcome = await container.orchestrator.handle_text(
        "Use file search on the assistant folder on my desktop"
    )

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call is not None
    assert outcome.call.skill_id == "windows.file_search"
    assert outcome.call.arguments == {
        "query": "resume",
        "root": "desktop/assistant",
        "limit": 20,
    }


@pytest.mark.asyncio
async def test_skill_preparation_error_is_returned_as_response(settings):
    container = build_container(settings)
    response, outcome = await container.orchestrator.handle_text("open application not-registered")
    assert response.severity == "warning"
    assert response.response_type == ResponseType.CLARIFICATION
    assert "registered" in response.display_text
    assert outcome.registration_requests
    assert outcome.registration_requests[0].application == "not-registered"


@pytest.mark.asyncio
async def test_preparation_error_is_remembered_for_follow_up(settings):
    container = build_container(settings)

    response, _ = await container.orchestrator.handle_text("open discord")
    assert "registered" in response.display_text

    planner = CapturingPlanner()
    container.orchestrator.planner = planner
    follow_up, _ = await container.orchestrator.handle_text("how do I register an application?")

    assert "Applications tab" in follow_up.display_text
    assert not planner.calls
    recent_context = tuple(container.orchestrator._session_context)
    assert recent_context[-2]["request"] == "open discord"
    assert recent_context[-2]["intent"] == "windows.launcher"
    assert recent_context[-2]["outcome"] == "awaiting_registration"
    assert "discord" in recent_context[-2]["response"]
    with container.database.session() as session:
        rows = session.query(HistoryRow).order_by(HistoryRow.id).all()
        assert "discord" in rows[0].response_text


@pytest.mark.asyncio
async def test_unregistered_application_returns_registration_request(settings):
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text("open discord")

    assert response.response_type == ResponseType.CLARIFICATION
    assert outcome.registration_requests
    assert outcome.registration_requests[0].call.skill_id == "windows.launcher"
    assert outcome.registration_requests[0].application == "discord"


@pytest.mark.asyncio
async def test_open_my_browser_resolves_registered_preference_memory(settings, tmp_path):
    container = build_container(settings)
    brave = tmp_path / "brave.exe"
    brave.write_bytes(b"")
    container.aliases.set("application", "brave browser", str(brave))
    container.memory_store.remember(
        type="preference",
        key="default_browser",
        value="Brave is my default browser.",
        source="user",
        confidence=1,
    )

    response, outcome = await container.orchestrator.handle_text("Open my browser")

    assert response.response_type == ResponseType.PERMISSION
    assert not outcome.registration_requests
    assert outcome.call is not None
    assert outcome.call.skill_id == "windows.launcher"
    assert outcome.call.arguments == {"application": "browser"}
    assert outcome.permission_requests[0].scope.targets == (str(brave.resolve()),)


@pytest.mark.asyncio
async def test_unregistered_website_name_requests_existing_browser_permission(settings):
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text("open youtube")

    assert response.response_type == ResponseType.PERMISSION
    assert not outcome.registration_requests
    assert outcome.permission_requests
    assert outcome.permission_requests[0].call.skill_id == "windows.browser_navigate"
    assert outcome.permission_requests[0].call.arguments == {
        "url": "https://www.youtube.com",
    }
    assert {request.scope.capability for request in outcome.permission_requests} == {
        "process.window",
        "input.control",
        "network",
    }
    assert any(
        request.scope.targets == ("https://www.youtube.com",)
        for request in outcome.permission_requests
    )


@pytest.mark.asyncio
async def test_slickdeals_result_request_uses_headless_browser_skill(settings):
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text(
        "Go to Slickdeals and give me the first deal"
    )

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call is not None
    assert outcome.call.skill_id == "web.headless_read"
    assert outcome.call.arguments["url"] == "https://slickdeals.net"
    assert outcome.call.arguments["extract"] == "deal"
    assert outcome.call.arguments["max_items"] == 1
    assert outcome.permission_requests[0].scope.capability == "network"
    assert outcome.permission_requests[0].scope.targets == ("https://slickdeals.net",)


@pytest.mark.asyncio
async def test_open_registered_application_requests_permission(settings, tmp_path):
    container = build_container(settings)
    target = tmp_path / "youtube.exe"
    target.write_bytes(b"")
    container.aliases.set("application", "youtube", str(target))

    response, outcome = await container.orchestrator.handle_text("open application youtube")

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.permission_requests
    assert outcome.permission_requests[0].call.skill_id == "windows.launcher"
    assert outcome.permission_requests[0].scope.capability == "process.launch"


@pytest.mark.asyncio
async def test_compound_launch_uses_registered_application_prefix(settings, tmp_path):
    container = build_container(settings)
    target = tmp_path / "brave.exe"
    target.write_bytes(b"")
    container.aliases.set("application", "brave browser", str(target))

    response, outcome = await container.orchestrator.handle_text(
        "open brave browser search for vlr.gg"
    )

    assert response.response_type == ResponseType.PERMISSION
    assert [request.call.skill_id for request in outcome.permission_requests] == [
        "windows.launcher",
    ]
    assert outcome.permission_requests[0].call.arguments == {
        "application": "brave browser",
        "url": "https://search.brave.com/search?q=vlr.gg",
    }
    assert {request.scope.capability for request in outcome.permission_requests} == {
        "process.launch",
    }


@pytest.mark.asyncio
async def test_browser_website_launch_keeps_url_in_permission_call(settings, tmp_path):
    container = build_container(settings)
    target = tmp_path / "brave.exe"
    target.write_bytes(b"")
    container.aliases.set("application", "brave", str(target))

    response, outcome = await container.orchestrator.handle_text("open brave and go to vlr.gg")

    assert response.response_type == ResponseType.PERMISSION
    assert len(outcome.permission_requests) == 1
    assert outcome.call.arguments == {
        "application": "brave",
        "url": "https://vlr.gg",
    }
    assert outcome.permission_requests[0].call.arguments == outcome.call.arguments


@pytest.mark.asyncio
async def test_private_browser_launch_keeps_private_in_permission_call(settings, tmp_path):
    container = build_container(settings)
    target = tmp_path / "brave.exe"
    target.write_bytes(b"")
    container.aliases.set("application", "brave", str(target))

    response, outcome = await container.orchestrator.handle_text("open private tab in brave")

    assert response.response_type == ResponseType.PERMISSION
    assert len(outcome.permission_requests) == 1
    assert outcome.call.arguments == {"application": "brave", "private": True}
    assert outcome.permission_requests[0].call.arguments == outcome.call.arguments


@pytest.mark.asyncio
async def test_private_browser_launch_with_website_keeps_url_and_private(settings, tmp_path):
    container = build_container(settings)
    target = tmp_path / "brave.exe"
    target.write_bytes(b"")
    container.aliases.set("application", "brave", str(target))

    response, outcome = await container.orchestrator.handle_text(
        "open a private brave tab with vlr.gg"
    )

    assert response.response_type == ResponseType.PERMISSION
    assert len(outcome.permission_requests) == 1
    assert outcome.call.arguments == {
        "application": "brave",
        "private": True,
        "url": "https://vlr.gg",
    }
    assert outcome.permission_requests[0].call.arguments == outcome.call.arguments


@pytest.mark.asyncio
async def test_default_browser_registration_handles_generic_private_tab(
    settings, tmp_path, monkeypatch
):
    target = tmp_path / "default-browser.exe"
    target.write_bytes(b"")
    monkeypatch.setattr(
        WindowsApplicationAdapter,
        "_default_browser_path",
        classmethod(lambda cls: str(target)),
    )
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text("open private tab")

    assert response.response_type == ResponseType.PERMISSION
    assert not outcome.registration_requests
    assert len(outcome.permission_requests) == 1
    assert outcome.call.arguments == {"application": "browser", "private": True}
    assert outcome.permission_requests[0].scope.targets == (str(target.resolve()),)


@pytest.mark.asyncio
async def test_default_browser_registration_handles_generic_tab(settings, tmp_path, monkeypatch):
    target = tmp_path / "default-browser.exe"
    target.write_bytes(b"")
    monkeypatch.setattr(
        WindowsApplicationAdapter,
        "_default_browser_path",
        classmethod(lambda cls: str(target)),
    )
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text("open a tab")

    assert response.response_type == ResponseType.PERMISSION
    assert not outcome.registration_requests
    assert {request.scope.capability for request in outcome.permission_requests} == {
        "process.window",
        "input.control",
    }
    assert outcome.call.arguments == {"operation": "new_tab", "target": "browser"}
    assert all(request.scope.targets == ("browser",) for request in outcome.permission_requests)


@pytest.mark.asyncio
async def test_browser_website_on_browser_phrase_keeps_url(settings, tmp_path):
    container = build_container(settings)
    target = tmp_path / "brave.exe"
    target.write_bytes(b"")
    container.aliases.set("application", "brave", str(target))

    response, outcome = await container.orchestrator.handle_text("open twitch.tv on brave")

    assert response.response_type == ResponseType.PERMISSION
    assert len(outcome.permission_requests) == 1
    assert outcome.call.arguments == {
        "application": "brave",
        "url": "https://twitch.tv",
    }


@pytest.mark.asyncio
async def test_browser_launch_strips_monitor_phrase_from_registration(settings, tmp_path):
    container = build_container(settings)
    target = tmp_path / "brave.exe"
    target.write_bytes(b"")
    container.aliases.set("application", "brave", str(target))

    response, outcome = await container.orchestrator.handle_text("open brave on my first monitor")

    assert response.response_type == ResponseType.PERMISSION
    assert len(outcome.permission_requests) == 2
    assert outcome.call.arguments == {"application": "brave"}
    assert [request.call.arguments for request in outcome.permission_requests] == [
        {"application": "brave"},
        {
            "operation": "move_to_monitor",
            "process_name": "brave.exe",
            "monitor": 1,
        },
    ]
    assert {request.scope.capability for request in outcome.permission_requests} == {
        "process.launch",
        "process.window",
    }


@pytest.mark.asyncio
async def test_compound_launch_reports_registration_before_follow_up_permission(settings):
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text("open brave and search for vlr.gg")

    assert response.response_type == ResponseType.CLARIFICATION
    assert outcome.registration_requests
    assert [request.application for request in outcome.registration_requests] == [
        "brave",
    ]
    assert outcome.registration_requests[0].call.arguments == {
        "application": "brave",
        "url": "https://search.brave.com/search?q=vlr.gg",
    }
    assert not outcome.permission_requests
    assert "registered" in response.display_text
