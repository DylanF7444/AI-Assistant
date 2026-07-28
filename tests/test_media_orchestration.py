from __future__ import annotations

import base64
from pathlib import Path

import pytest

from assistant.bootstrap import build_container
from assistant.contracts import MediaAction, ResponseType
from assistant.policy.permissions import GrantType

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def grant_media_read(container, target, *, session_id="desktop"):
    container.permissions.grant(
        user_id="local-user",
        skill_id="media.present",
        capability="filesystem.read",
        grant_type=GrantType.SESSION,
        targets=(str(target),),
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_camera_roll_request_prompts_for_scoped_folder_permission(settings):
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text(
        "Pull up an image from my camera roll"
    )

    expected_root = (Path.home() / "Pictures" / "Camera Roll").resolve()
    assert response.response_type == ResponseType.PERMISSION
    assert len(outcome.permission_requests) == 1
    request = outcome.permission_requests[0]
    assert request.call.skill_id == "media.present"
    assert request.call.arguments == {
        "root": str(expected_root),
        "kind_hint": "image",
        "autoplay": False,
        "limit": 20,
    }
    assert request.scope.capability == "filesystem.read"
    assert request.scope.targets == (str(expected_root),)


@pytest.mark.asyncio
async def test_exact_media_request_prepares_viewer_then_accepts_controls(
    settings,
    tmp_path,
):
    path = tmp_path / "photo.png"
    path.write_bytes(_PNG)
    container = build_container(settings)
    grant_media_read(container, path.resolve())

    response, outcome = await container.orchestrator.handle_text(f"show {path}")

    assert outcome.result is not None
    assert response.media_directive is not None
    assert response.media_directive.action == MediaAction.SHOW
    assert response.media_directive.source.location == str(path.resolve())
    assert container.orchestrator.state.last_media_path == str(path.resolve())

    control_response, control_outcome = await container.orchestrator.handle_text("pause it")
    assert control_outcome.result is not None
    assert control_response.media_directive.action == MediaAction.PAUSE
    assert (
        control_response.media_directive.presentation_id == response.media_directive.presentation_id
    )


@pytest.mark.asyncio
async def test_media_reference_uses_numbered_file_candidates_without_guessing(
    settings,
    tmp_path,
):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(_PNG)
    second.write_bytes(_PNG)
    container = build_container(settings)
    container.orchestrator.remember_file_candidates([str(first), str(second)])
    grant_media_read(container, second.resolve())

    response, outcome = await container.orchestrator.handle_text("pull up the second result")

    assert outcome.result is not None
    assert response.media_directive.source.location == str(second.resolve())
    assert container.orchestrator.state.selected_file == str(second)


@pytest.mark.asyncio
async def test_named_media_search_returns_candidates_and_remembers_them(
    settings,
    tmp_path,
):
    first = tmp_path / "vacation-one.png"
    second = tmp_path / "vacation-two.png"
    first.write_bytes(_PNG)
    second.write_bytes(_PNG)
    container = build_container(settings)
    grant_media_read(container, tmp_path.resolve())

    response, outcome = await container.orchestrator.handle_text(
        f"show vacation photo in {tmp_path}"
    )

    assert outcome.result is not None
    assert outcome.result.error.code == "clarification_required"
    assert response.response_type == ResponseType.CLARIFICATION
    assert container.orchestrator.state.file_candidates == (
        str(first.resolve()),
        str(second.resolve()),
    )


@pytest.mark.asyncio
async def test_unresolved_and_inactive_media_requests_clarify_without_execution(
    settings,
):
    container = build_container(settings)

    unresolved, unresolved_outcome = await container.orchestrator.handle_text("play meeting.mp3")
    inactive, inactive_outcome = await container.orchestrator.handle_text("pause it")

    assert unresolved.response_type == ResponseType.CLARIFICATION
    assert "folder" in unresolved.display_text.casefold()
    assert unresolved_outcome.result is None
    assert inactive.response_type == ResponseType.CLARIFICATION
    assert inactive_outcome.result is None


@pytest.mark.asyncio
async def test_close_and_forget_clear_active_media_context(settings, tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(_PNG)
    container = build_container(settings)
    grant_media_read(container, path.resolve())
    shown, _ = await container.orchestrator.handle_text(f"show {path}")

    closed, _ = await container.orchestrator.handle_text("close the viewer")
    inactive, _ = await container.orchestrator.handle_text("pause it")

    assert closed.media_directive.action == MediaAction.CLOSE
    assert inactive.response_type == ResponseType.CLARIFICATION
    await container.orchestrator.handle_text("forget this session")
    assert container.orchestrator.state.active_presentation_id is None
    assert container.orchestrator.state.last_media_path is None
    assert container.media_sessions.current() is None
    assert shown.media_directive is not None
