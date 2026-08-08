from uuid import uuid4

from assistant.contracts import (
    MediaAction,
    MediaDirective,
    RequestResultItem,
    ResponseType,
    ResultStatus,
    SkillError,
    SkillResult,
)
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


def test_failure_includes_error_details_in_normal_mode():
    response = response_from_result(
        SkillResult(
            status=ResultStatus.FAILED,
            summary="The action failed.",
            error=SkillError(
                code="skill_failed",
                message="Changing Wi-Fi requires administrator privileges.",
            ),
        )
    )
    assert "The action failed." in response.display_text
    assert "skill_failed: Changing Wi-Fi requires administrator privileges." in (
        response.display_text
    )


def test_clarification_error_uses_clarification_response_type():
    response = response_from_result(
        SkillResult(
            status=ResultStatus.DENIED,
            summary="Which browser window should I use?",
            error=SkillError(
                code="clarification_required",
                message="Both private and regular windows are open.",
                recoverable=True,
            ),
        )
    )

    assert response.response_type == ResponseType.CLARIFICATION
    assert response.severity == "warning"


def test_detailed_mode_and_sensitive_display_privacy():
    result = SkillResult(
        status=ResultStatus.COMPLETED,
        summary="Result ready.",
        data={"value": 42},
    )
    assert '"value": 42' in response_from_result(result, detail="detailed").display_text
    hidden = response_from_result(
        result.model_copy(update={"sensitive": True}),
        detail="detailed",
        hide_sensitive_display=True,
    )
    assert "42" not in hidden.display_text


def test_detailed_mode_keeps_skill_display_text_human_friendly():
    response = response_from_result(
        SkillResult(
            status=ResultStatus.COMPLETED,
            summary="I couldn't find any calendar events.",
            display_text="You don't have any upcoming events on your calendar.",
            data={"operation": "list_events", "resources": []},
        ),
        detail="detailed",
    )

    assert response.display_text == "You don't have any upcoming events on your calendar."


def test_response_mapping_keeps_media_directive():
    directive = MediaDirective(
        presentation_id=uuid4(),
        action=MediaAction.PAUSE,
    )

    response = response_from_result(
        SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Paused.",
            media_directive=directive,
        )
    )

    assert response.media_directive == directive


def test_response_mapping_preserves_and_presents_request_result_items():
    item = RequestResultItem(
        item_id="task-7",
        title="Review release notes",
        value={"id": 7},
        kind="task",
        description="Open",
    )

    response = response_from_result(
        SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Found one task.",
            items=(item,),
        )
    )

    assert response.items == (item,)
    assert response.display_text == "Found one task.\n1. Review release notes — Open"


def test_response_mapping_adds_sanitized_web_citations_from_result_items():
    item = RequestResultItem(
        item_id="result-1",
        title="Primary source",
        value="summary",
        kind="web_result",
        metadata={
            "url": "https://example.com/report?token=secret#private",
            "source": "Example Research",
            "namespace": "web",
        },
    )

    response = response_from_result(
        SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Found one source.",
            items=(item,),
        )
    )

    assert len(response.citations) == 1
    assert response.citations[0].url == "https://example.com/report"
    assert "token" not in response.model_dump_json()


def test_sensitive_results_do_not_expose_item_citations():
    item = RequestResultItem(
        item_id="private-1",
        title="Private",
        value="private",
        metadata={"url": "https://example.com/private"},
    )

    response = response_from_result(
        SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Private result.",
            items=(item,),
            sensitive=True,
        )
    )

    assert response.citations == ()
