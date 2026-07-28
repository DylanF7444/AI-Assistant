from __future__ import annotations

import asyncio
import base64

import pytest

from assistant.bootstrap import build_container
from assistant.contracts import (
    BackgroundMode,
    MediaAction,
    MediaKind,
    PermissionContext,
    ResultStatus,
    SkillCall,
)
from assistant.policy.permissions import GrantType
from assistant.presentation.media_resolver import MediaResolver
from assistant.presentation.session import (
    MediaSessionRegistry,
    MediaSessionStatus,
)
from assistant.skills.media import MediaPresentSkill, MediaViewerControlSkill

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def call(skill_id: str, arguments: dict) -> SkillCall:
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="media-test"),
    )


@pytest.mark.asyncio
async def test_media_present_skill_prepares_typed_directive_and_exact_scope(tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(_PNG)
    sessions = MediaSessionRegistry()
    skill = MediaPresentSkill(MediaResolver(), sessions)
    skill_call = call("media.present", {"source": str(path), "autoplay": False})

    scopes = skill.required_permissions(skill_call)
    result = await skill.execute(skill_call, asyncio.Event())

    assert scopes[0].capability == "filesystem.read"
    assert scopes[0].targets == (str(path.resolve()),)
    assert result.status == ResultStatus.COMPLETED
    assert result.media_directive is not None
    assert result.media_directive.action == MediaAction.SHOW
    assert result.media_directive.source is not None
    assert result.media_directive.source.kind == MediaKind.IMAGE
    assert sessions.current().presentation_id == result.media_directive.presentation_id
    assert skill.manifest.background_mode == BackgroundMode.FOREGROUND_ONLY


@pytest.mark.asyncio
async def test_media_search_returns_selectable_paths_instead_of_guessing(tmp_path):
    for name in ("vacation-one.png", "vacation-two.png"):
        (tmp_path / name).write_bytes(_PNG)
    skill = MediaPresentSkill(MediaResolver(), MediaSessionRegistry())

    result = await skill.execute(
        call(
            "media.present",
            {"query": "vacation", "root": str(tmp_path)},
        ),
        asyncio.Event(),
    )

    assert result.status == ResultStatus.DENIED
    assert result.error.code == "clarification_required"
    assert len(result.data["paths"]) == 2
    assert result.media_directive is None


@pytest.mark.asyncio
async def test_media_folder_browse_returns_images_without_inventing_a_filename_query(tmp_path):
    for name in ("IMG_1001.png", "summer-memory.png"):
        (tmp_path / name).write_bytes(_PNG)
    (tmp_path / "notes.txt").write_text("not media", encoding="utf-8")
    skill = MediaPresentSkill(MediaResolver(), MediaSessionRegistry())

    result = await skill.execute(
        call(
            "media.present",
            {"root": str(tmp_path), "kind_hint": "image"},
        ),
        asyncio.Event(),
    )

    assert result.status == ResultStatus.DENIED
    assert result.error.code == "clarification_required"
    assert result.data["paths"] == [
        str((tmp_path / "IMG_1001.png").resolve()),
        str((tmp_path / "summer-memory.png").resolve()),
    ]


@pytest.mark.asyncio
async def test_viewer_control_targets_only_the_active_presentation(tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(_PNG)
    sessions = MediaSessionRegistry()
    present = MediaPresentSkill(MediaResolver(), sessions)
    prepared = await present.execute(
        call("media.present", {"source": str(path)}),
        asyncio.Event(),
    )
    control = MediaViewerControlSkill(sessions)

    result = await control.execute(
        call("media.viewer_control", {"action": "fullscreen"}),
        asyncio.Event(),
    )

    assert result.media_directive.action == MediaAction.FULLSCREEN
    assert result.media_directive.presentation_id == prepared.media_directive.presentation_id
    sessions.update(
        prepared.media_directive.presentation_id,
        status=MediaSessionStatus.CLOSED,
    )
    with pytest.raises(ValueError, match="no active"):
        await control.execute(
            call("media.viewer_control", {"action": "play"}),
            asyncio.Event(),
        )


@pytest.mark.asyncio
async def test_execution_engine_enforces_media_read_permission_and_file_policy(
    settings,
    tmp_path,
):
    path = tmp_path / "photo.png"
    path.write_bytes(_PNG)
    container = build_container(settings)
    skill_call = call("media.present", {"source": str(path)})

    missing = await container.engine.prepare(skill_call)

    assert missing.permission_requests
    assert missing.permission_requests[0].scope.targets == (str(path.resolve()),)
    container.permissions.grant(
        user_id="local-user",
        skill_id="media.present",
        capability="filesystem.read",
        grant_type=GrantType.SESSION,
        targets=(str(path.resolve()),),
        session_id="media-test",
    )
    completed = await container.engine.prepare(skill_call)
    assert completed.result.status == ResultStatus.COMPLETED
    assert completed.result.media_directive is not None

    container.file_access_policy.update_entry(
        list_name="blacklist",
        access="read",
        path=str(path),
        add=True,
    )
    blocked = await container.engine.prepare(call("media.present", {"source": str(path)}))
    assert blocked.result.error.code == "file_policy_denied"


def test_bootstrap_registers_separate_global_and_in_app_media_skills(settings):
    container = build_container(settings)

    assert container.registry.get("windows.media")
    assert container.registry.get("media.present")
    assert container.registry.get("media.viewer_control")
    assert container.media_sessions.current() is None
    assert container.media_cache.cache_dir == (settings.data_dir / "media-cache").resolve()
