from assistant.contracts import ResponseType, ResultStatus, SkillResult
from assistant.response import response_from_result


def test_sensitive_result_is_not_spoken():
    response = response_from_result(
        SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Secret text",
            sensitive=True,
        )
    )
    assert response.display_text == "Secret text"
    assert response.response_type == ResponseType.SKILL
    assert "Secret text" not in response.speech_text


def test_failure_never_claims_success():
    response = response_from_result(
        SkillResult(status=ResultStatus.FAILED, summary="Could not open the file.")
    )
    assert response.severity == "error"
    assert "Could not" in response.display_text


def test_detailed_mode_and_sensitive_display_privacy():
    result = SkillResult(
        status=ResultStatus.COMPLETED,
        summary="Result ready.",
        data={"value": 42},
    )
    assert '"value": 42' in response_from_result(
        result, detail="detailed"
    ).display_text
    hidden = response_from_result(
        result.model_copy(update={"sensitive": True}),
        detail="detailed",
        hide_sensitive_display=True,
    )
    assert "42" not in hidden.display_text
