import asyncio

import pytest
from jsonschema import ValidationError
from pathlib import Path
from uuid import uuid4

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
)
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


@pytest.mark.asyncio
async def test_text_to_command_to_response(settings):
    container = build_container(settings)
    response, outcome = await container.orchestrator.handle_text("calculate 4 times 5")
    assert outcome.result.status == ResultStatus.COMPLETED
    assert response.response_type == ResponseType.SKILL
    assert "20" in response.display_text
    assert "Request breakdown:" not in response.display_text


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

    response, outcome = await container.orchestrator.handle_text(
        f"find resume in {documents}"
    )
    assert outcome.result.status == ResultStatus.COMPLETED
    assert "1." in response.display_text
    assert str(source.resolve()) in response.display_text

    response, _ = await container.orchestrator.handle_text("select 1")
    assert str(source.resolve()) in response.display_text

    response, outcome = await container.orchestrator.handle_text(
        f"copy it to {destination_folder}"
    )
    assert response.response_type == ResponseType.PERMISSION
    assert len(outcome.permission_requests) == 2
    assert [request.scope.capability for request in outcome.permission_requests] == [
        "filesystem.read",
        "filesystem.write",
    ]
    assert outcome.permission_requests[0].scope.targets == (str(source.resolve()),)
    assert outcome.permission_requests[1].scope.targets == (
        str(destination.resolve()),
    )

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
async def test_file_search_result_ordinal_copy_uses_named_documents_folder(
    settings, tmp_path
):
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

    response, outcome = await container.orchestrator.handle_text(
        f"find resume in {search_root}"
    )
    assert outcome.result.status == ResultStatus.COMPLETED
    assert str(source.resolve()) in response.display_text

    response, outcome = await container.orchestrator.handle_text(
        "Copy and paste the first result into my documents folder"
    )

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call.skill_id == "windows.file_operation"
    assert outcome.call.arguments["source"] == str(source.resolve())
    assert outcome.call.arguments["destination"] == str(
        expected_destination.resolve()
    )
    assert outcome.permission_requests[0].scope.targets == (str(source.resolve()),)
    assert outcome.permission_requests[1].scope.targets == (
        str(expected_destination.resolve()),
    )


@pytest.mark.asyncio
async def test_file_search_result_ordinal_open_uses_matching_candidate(
    settings, tmp_path
):
    container = build_container(settings)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one")
    second.write_text("two")
    container.orchestrator.remember_file_candidates(
        [str(first.resolve()), str(second.resolve())]
    )

    response, outcome = await container.orchestrator.handle_text(
        "Open the second one"
    )

    assert response.response_type == ResponseType.PERMISSION
    assert outcome.call.skill_id == "windows.file_operation"
    assert outcome.call.arguments == {
        "operation": "open",
        "source": str(second.resolve()),
    }
    assert outcome.permission_requests[0].scope.targets == (str(second.resolve()),)


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

    first, _ = await container.orchestrator.handle_text(
        "convert a few miles to kilometers"
    )
    assert first.response_type == ResponseType.CLARIFICATION

    response, outcome = await container.orchestrator.handle_text(
        "new request calculate 2 plus 3"
    )

    assert response.response_type == ResponseType.SKILL
    assert outcome.call.skill_id == "core.calculator"
    assert outcome.result.status == ResultStatus.COMPLETED
    assert "5" in response.display_text


@pytest.mark.asyncio
async def test_cancel_clears_pending_clarification(settings):
    container = build_container(settings)

    first, _ = await container.orchestrator.handle_text(
        "convert a few miles to kilometers"
    )
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

    response, outcome = await container.orchestrator.listen_once(
        PartialThenFinalSTT(), audio()
    )

    assert "2" in response.display_text
    assert outcome.result is not None


@pytest.mark.asyncio
async def test_empty_voice_transcript_reports_no_speech(settings):
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
    assert outcome.result is None


@pytest.mark.asyncio
async def test_session_context_is_bounded_correctable_and_forgettable(settings):
    container = build_container(settings)
    await container.orchestrator.handle_text("calculate 1 plus 1")
    await container.orchestrator.handle_text("calculate 2 plus 2")
    assert len(container.orchestrator._session_context) == 2
    corrected, _ = await container.orchestrator.handle_text(
        "correct that to calculate 3 plus 4"
    )
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
    response, outcome = await container.orchestrator.handle_text(
        "do something unmatched"
    )
    assert response.severity == "warning"
    assert "Deterministic commands still work" in response.display_text
    assert outcome.result is None


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
async def test_set_my_device_volume_does_not_use_local_planner(settings):
    container = build_container(settings)
    container.orchestrator.planner = BrokenPlanner()

    response, outcome = await container.orchestrator.handle_text(
        "Set my device volume to 80"
    )

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

    response, outcome = await container.orchestrator.handle_text(
        "do something unmatched"
    )

    assert "invalid planner response" in response.display_text
    assert "Start Ollama" not in response.display_text
    assert outcome.result is None


@pytest.mark.asyncio
async def test_invalid_planner_output_includes_debug_response(settings):
    container = build_container(settings)
    container.orchestrator.planner = InvalidPlannerWithResponse()

    response, outcome = await container.orchestrator.handle_text(
        "do something unmatched"
    )

    assert "invalid planner response" in response.display_text
    assert "Debug invalid planner response" in response.display_text
    assert '"answer": "I can help with that."' in response.display_text
    assert "Validation error: 'confidence' is a required property" in response.display_text
    assert outcome.result is None


@pytest.mark.asyncio
async def test_planner_timeout_does_not_claim_ollama_is_stopped(settings):
    container = build_container(settings)
    container.orchestrator.planner = SlowPlanner()

    response, outcome = await container.orchestrator.handle_text(
        "do something unmatched"
    )

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
async def test_skill_preparation_error_is_returned_as_response(settings):
    container = build_container(settings)
    response, outcome = await container.orchestrator.handle_text(
        "open application not-registered"
    )
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
    follow_up, _ = await container.orchestrator.handle_text(
        "how do I register an application?"
    )

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

    response, outcome = await container.orchestrator.handle_text(
        "open brave and go to vlr.gg"
    )

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

    response, outcome = await container.orchestrator.handle_text(
        "open private tab in brave"
    )

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
async def test_default_browser_registration_handles_generic_tab(
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

    response, outcome = await container.orchestrator.handle_text("open a tab")

    assert response.response_type == ResponseType.PERMISSION
    assert not outcome.registration_requests
    assert len(outcome.permission_requests) == 1
    assert outcome.call.arguments == {"application": "browser"}
    assert outcome.permission_requests[0].scope.targets == (str(target.resolve()),)


@pytest.mark.asyncio
async def test_browser_website_on_browser_phrase_keeps_url(settings, tmp_path):
    container = build_container(settings)
    target = tmp_path / "brave.exe"
    target.write_bytes(b"")
    container.aliases.set("application", "brave", str(target))

    response, outcome = await container.orchestrator.handle_text(
        "open twitch.tv on brave"
    )

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

    response, outcome = await container.orchestrator.handle_text(
        "open brave on my first monitor"
    )

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

    response, outcome = await container.orchestrator.handle_text(
        "open brave and search for vlr.gg"
    )

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
