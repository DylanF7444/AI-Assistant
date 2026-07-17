from pydantic import BaseModel

from assistant.contracts import BackgroundMode, PermissionScope
from assistant.policy.background import background_compatibility
from assistant.skills.builtin import manifest


class _Empty(BaseModel):
    pass


def _manifest(*, mode: BackgroundMode = BackgroundMode.CAPABILITY_GATED):
    return manifest(
        skill_id="test.background_policy",
        display_name="Background policy test",
        description="Test background compatibility.",
        input_model=_Empty,
        output_model=_Empty,
        background_mode=mode,
    )


def test_background_policy_allows_noninteractive_capabilities():
    decision = background_compatibility(
        _manifest(),
        (
            PermissionScope(
                capability="filesystem.read",
                targets=("C:/data",),
                reason="Read approved files.",
            ),
            PermissionScope(
                capability="network",
                targets=("https://example.com",),
                reason="Read a public page.",
            ),
        ),
    )

    assert decision.allowed
    assert decision.blocking_capabilities == ()


def test_background_policy_blocks_focus_and_device_capabilities():
    decision = background_compatibility(
        _manifest(),
        (
            PermissionScope(
                capability="input.control",
                targets=("browser",),
                reason="Send keys.",
            ),
            PermissionScope(
                capability="system.display",
                targets=("brightness",),
                reason="Change brightness.",
            ),
        ),
    )

    assert not decision.allowed
    assert decision.blocking_capabilities == ("input.control", "system.display")
    assert "active desktop or device" in decision.reason


def test_unreviewed_manifest_defaults_to_foreground_only():
    guarded = _manifest(mode=BackgroundMode.FOREGROUND_ONLY)
    explicit = _manifest(mode=BackgroundMode.SAFE)

    assert not background_compatibility(guarded, ()).allowed
    assert background_compatibility(explicit, ()).allowed
