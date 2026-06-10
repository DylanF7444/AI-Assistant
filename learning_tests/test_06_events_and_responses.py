from uuid import uuid4

import pytest

from assistant.contracts import (
    PipelineEvent,
    PipelineState,
    ResultStatus,
    SkillResult,
)
from assistant.events import EventBus
from assistant.response import response_from_result


@pytest.mark.asyncio
async def test_event_bus_keeps_history_by_correlation_id():
    bus = EventBus()
    correlation_id = uuid4()
    event = PipelineEvent(
        correlation_id=correlation_id,
        state=PipelineState.THINKING,
        message="Routing command",
    )

    await bus.publish(event)

    assert bus.history(correlation_id) == (event,)
    assert bus.history(uuid4()) == ()


def test_completed_skill_result_becomes_a_success_response():
    result = SkillResult(
        status=ResultStatus.COMPLETED,
        summary="Calculation complete.",
        data={"value": 42},
    )

    response = response_from_result(result, detail="detailed")

    assert response.severity == "success"
    assert '"value": 42' in response.display_text
    assert response.speak is True


def test_sensitive_result_uses_safe_speech_and_can_hide_the_display():
    result = SkillResult(
        status=ResultStatus.COMPLETED,
        summary="Clipboard content read.",
        display_text="private clipboard text",
        sensitive=True,
    )

    response = response_from_result(result, hide_sensitive_display=True)

    assert response.display_text == "Sensitive result hidden by privacy settings."
    assert "See the screen" in response.speech_text
