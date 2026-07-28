from uuid import uuid4

import pytest
from pydantic import ValidationError

from assistant.contracts import (
    MediaAction,
    MediaDirective,
    MediaKind,
    MediaSource,
    MediaSourceType,
    Response,
    ResultStatus,
    SkillResult,
)
from assistant.execution.engine import ExecutionEngine
from assistant.response import response_from_result


def media_source() -> MediaSource:
    return MediaSource(
        source_type=MediaSourceType.LOCAL_FILE,
        location=r"C:\Users\Example\Pictures\photo.png",
        kind=MediaKind.IMAGE,
        mime_type="image/png",
        title="photo.png",
        size_bytes=123,
        fingerprint="a" * 64,
        width=10,
        height=20,
    )


def test_show_directive_is_strict_immutable_and_versioned():
    directive = MediaDirective(
        presentation_id=uuid4(),
        action=MediaAction.SHOW,
        source=media_source(),
    )

    assert directive.contract_version == "1.0"
    assert directive.source is not None
    assert directive.source.kind == MediaKind.IMAGE
    with pytest.raises(ValidationError):
        directive.autoplay = True
    with pytest.raises(ValidationError):
        MediaDirective(
            presentation_id=uuid4(),
            action=MediaAction.SHOW,
            source=media_source(),
            unexpected=True,
        )


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        (MediaAction.SEEK_RELATIVE, {}),
        (MediaAction.SEEK_RELATIVE, {"delta_seconds": 0}),
        (MediaAction.SEEK_ABSOLUTE, {}),
        (MediaAction.SET_VOLUME, {}),
        (MediaAction.SET_VOLUME, {"volume": 2}),
        (MediaAction.SET_RATE, {"playback_rate": 0.1}),
        (MediaAction.PAUSE, {"position_seconds": 1}),
        (MediaAction.PLAY, {"source": media_source()}),
        (MediaAction.CLOSE, {"autoplay": True}),
    ],
)
def test_media_directive_rejects_action_field_mismatches(action, arguments):
    with pytest.raises(ValidationError):
        MediaDirective(
            presentation_id=uuid4(),
            action=action,
            **arguments,
        )


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        (MediaAction.PLAY, {}),
        (MediaAction.PAUSE, {}),
        (MediaAction.SEEK_RELATIVE, {"delta_seconds": -30}),
        (MediaAction.SEEK_ABSOLUTE, {"position_seconds": 12.5}),
        (MediaAction.SET_VOLUME, {"volume": 0.5}),
        (MediaAction.SET_RATE, {"playback_rate": 1.5}),
        (MediaAction.FULLSCREEN, {}),
        (MediaAction.CLOSE, {}),
    ],
)
def test_media_control_directives_accept_only_their_fields(action, arguments):
    directive = MediaDirective(
        presentation_id=uuid4(),
        action=action,
        **arguments,
    )

    assert directive.action == action


def test_media_source_rejects_control_characters_and_bad_fingerprint():
    with pytest.raises(ValidationError):
        MediaSource.model_validate(
            {
                **media_source().model_dump(),
                "title": "bad\nname",
                "fingerprint": "not-a-fingerprint",
            }
        )
    with pytest.raises(ValidationError):
        MediaSource(
            source_type=MediaSourceType.LOCAL_FILE,
            location="bad\npath",
            kind=MediaKind.IMAGE,
            mime_type="image/png",
            title="photo.png",
            size_bytes=1,
            fingerprint="a" * 64,
        )


def test_remote_media_source_requires_query_free_https_provenance():
    values = {
        "source_type": MediaSourceType.REMOTE_CACHE,
        "location": r"C:\cache\photo.png",
        "kind": MediaKind.IMAGE,
        "mime_type": "image/png",
        "title": "photo.png",
        "size_bytes": 1,
        "fingerprint": "a" * 64,
    }

    with pytest.raises(ValidationError, match="provenance"):
        MediaSource(**values)
    with pytest.raises(ValidationError, match="sanitized"):
        MediaSource(
            **values,
            origin="https://example.com/photo.png?token=private",
        )

    source = MediaSource(
        **values,
        origin="https://example.com/photo.png",
    )
    assert source.origin == "https://example.com/photo.png"


def test_response_mapping_preserves_media_directive():
    directive = MediaDirective(
        presentation_id=uuid4(),
        action=MediaAction.SHOW,
        source=media_source(),
        autoplay=True,
    )
    result = SkillResult(
        status=ResultStatus.COMPLETED,
        summary="Prepared photo.png.",
        media_directive=directive,
    )

    response = response_from_result(result)

    assert response.media_directive == directive
    assert Response.model_validate(response.model_dump()).media_directive == directive


def test_plan_aggregation_preserves_last_media_directive():
    first = MediaDirective(
        presentation_id=uuid4(),
        action=MediaAction.SHOW,
        source=media_source(),
    )
    second = MediaDirective(
        presentation_id=first.presentation_id,
        action=MediaAction.FULLSCREEN,
    )

    result = ExecutionEngine._aggregate_plan_results(
        {
            "show": SkillResult(
                status=ResultStatus.COMPLETED,
                summary="Shown.",
                media_directive=first,
            ),
            "fullscreen": SkillResult(
                status=ResultStatus.COMPLETED,
                summary="Fullscreen.",
                media_directive=second,
            ),
        }
    )

    assert result.media_directive == second
