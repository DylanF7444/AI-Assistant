import asyncio

import pytest
from jsonschema import ValidationError
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
from assistant.language.planner import InvalidPlannerResponse
from assistant.storage.database import HistoryRow


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


@pytest.mark.asyncio
async def test_text_to_command_to_response(settings):
    container = build_container(settings)
    response, outcome = await container.orchestrator.handle_text("calculate 4 times 5")
    assert outcome.result.status == ResultStatus.COMPLETED
    assert response.response_type == ResponseType.SKILL
    assert "20" in response.display_text
    assert "Request breakdown:" not in response.display_text


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
async def test_sensitive_action_is_denied_without_permission(settings):
    container = build_container(settings)
    response, outcome = await container.orchestrator.handle_text("play music")
    assert outcome.result.status == ResultStatus.DENIED
    assert outcome.permission_requests
    assert "media.control" in response.display_text
    assert "Grant Permission" in response.display_text
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
async def test_open_registered_application_requests_permission(settings, tmp_path):
    container = build_container(settings)
    target = tmp_path / "youtube.exe"
    target.write_bytes(b"")
    container.aliases.set("application", "youtube", str(target))

    response, outcome = await container.orchestrator.handle_text("open youtube")

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
