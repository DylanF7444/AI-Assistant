import asyncio

import pytest

from assistant.bootstrap import build_container
from assistant.contracts import PermissionContext, ResultStatus, SkillCall
from assistant.diagnostics import multimedia_report


class UnhealthyProvider:
    async def health(self):
        return {"healthy": False, "error": "model missing"}


@pytest.mark.asyncio
async def test_diagnostic_skill_returns_actionable_provider_guidance(settings):
    container = build_container(settings)
    skill = container.registry.get("core.diagnostic")
    skill.providers["stt"] = UnhealthyProvider()
    result = await skill.execute(
        SkillCall(
            skill_id="core.diagnostic",
            arguments={"include_skills": False},
            permission_context=PermissionContext(session_id="test"),
        ),
        asyncio.Event(),
    )
    assert result.status == ResultStatus.COMPLETED
    assert any("STT" in item for item in result.data["guidance"])
    assert "STT is unavailable: model missing" in result.display_text


def test_multimedia_diagnostics_are_bounded_and_do_not_expose_paths(settings):
    container = build_container(settings)
    report = multimedia_report(
        media_cache=container.media_cache,
        media_sessions=container.media_sessions,
    )

    assert isinstance(report["available"], bool)
    assert isinstance(report["image_formats"], tuple)
    assert isinstance(report["audio_video_formats"], tuple)
    assert isinstance(report["output_devices"], tuple)
    assert report["cache_bytes"] == 0
    assert report["last_error_code"] is None
    assert str(settings.data_dir) not in str(report)
