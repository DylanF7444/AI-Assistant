import pytest
from uuid import uuid4

from assistant.bootstrap import build_container
from assistant.contracts import (
    PipelineEvent,
    PipelineState,
    ResultStatus,
    Transcript,
    WordTimestamp,
)


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


class BrokenPlanner:
    async def plan(self, text, recent_context=()):
        raise ConnectionError("connection refused")

    async def cancel(self):
        pass


class AnsweringPlanner:
    async def plan(self, text, recent_context=()):
        from assistant.contracts import Intent

        return Intent(
            name="conversation.answer",
            arguments={"text": "A direct answer."},
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
    assert "20" in response.display_text


@pytest.mark.asyncio
async def test_sensitive_action_is_denied_without_permission(settings):
    container = build_container(settings)
    response, outcome = await container.orchestrator.handle_text("play music")
    assert outcome.result.status == ResultStatus.DENIED
    assert outcome.permission_requests
    assert "media.control" in response.display_text
    assert "Grant Permission" in response.display_text
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
async def test_conversational_answer_does_not_execute_a_skill(settings):
    container = build_container(settings)
    container.orchestrator.planner = AnsweringPlanner()
    response, outcome = await container.orchestrator.handle_text("Tell me something")
    assert response.display_text == "A direct answer."
    assert outcome.result is None


@pytest.mark.asyncio
async def test_skill_preparation_error_is_returned_as_response(settings):
    container = build_container(settings)
    response, outcome = await container.orchestrator.handle_text(
        "open application not-registered"
    )
    assert response.severity == "warning"
    assert "not registered" in response.display_text
    assert outcome.result is None
