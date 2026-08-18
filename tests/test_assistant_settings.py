from __future__ import annotations

import pytest

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.contracts import PermissionContext, ResponseType, ResultStatus, SkillCall
from assistant.policy.permissions import GrantType
from assistant.router.deterministic import DeterministicRouter
from assistant.storage.database import SettingsStore


def _call(setting: str, value, *, key: str) -> SkillCall:
    return SkillCall(
        skill_id="core.settings",
        arguments={"operation": "set", "setting": setting, "value": value},
        permission_context=PermissionContext(session_id="settings-test"),
        idempotency_key=key,
    )


def _grant_setting(container, setting: str) -> None:
    container.permissions.grant(
        user_id="local-user",
        skill_id="core.settings",
        capability="settings.set",
        grant_type=GrantType.ALWAYS,
        targets=(setting,),
    )


@pytest.mark.asyncio
async def test_safe_assistant_setting_change_uses_scoped_permission_and_persists(settings):
    container = build_container(settings)
    call = _call("response_detail", "detailed", key="response-detail")

    missing = await container.engine.prepare(call)

    assert missing.permission_requests[0].scope.capability == "settings.set"
    assert missing.permission_requests[0].scope.targets == ("response_detail",)
    assert container.settings.response_detail == "normal"

    _grant_setting(container, "response_detail")
    completed = await container.engine.prepare(call)

    assert completed.confirmation is None
    assert completed.result.status == ResultStatus.COMPLETED
    assert completed.result.data["previous_value"] == "normal"
    assert completed.result.data["value"] == "detailed"
    assert completed.result.data["requires_confirmation"] is False
    assert container.settings.response_detail == "detailed"
    assert container.orchestrator.response_detail == "detailed"
    assert SettingsStore(container.database).get("application_settings")["response_detail"] == (
        "detailed"
    )

    restored = build_container(Settings(profile="testing", data_dir=settings.data_dir))
    assert restored.settings.response_detail == "detailed"


@pytest.mark.asyncio
async def test_sensitive_setting_waits_for_exact_confirmation_before_change(settings):
    container = build_container(settings)
    _grant_setting(container, "offline_lock")
    call = _call("offline_lock", False, key="disable-offline-lock")

    prepared = await container.engine.prepare(call)

    assert prepared.confirmation is not None
    assert prepared.confirmation.exact_targets == ("offline_lock",)
    assert "offline lock" in prepared.confirmation.exact_action
    assert container.settings.offline_lock is True

    completed = await container.engine.execute(
        call,
        confirmation_id=prepared.confirmation.confirmation_id,
    )

    assert completed.status == ResultStatus.COMPLETED
    assert completed.data["requires_confirmation"] is True
    assert container.settings.offline_lock is False
    assert container.dashboard.offline_lock is False
    assert container.extension_hub.offline_lock is False
    assert container.media_cache.offline_lock is False


@pytest.mark.asyncio
async def test_assistant_request_routes_permission_then_confirmation(settings):
    container = build_container(settings)

    permission_response, permission_outcome = await container.orchestrator.handle_text(
        "disable offline mode"
    )

    assert permission_response.response_type == ResponseType.PERMISSION
    assert permission_outcome.permission_requests[0].scope.targets == ("offline_lock",)
    assert container.settings.offline_lock is True

    _grant_setting(container, "offline_lock")
    confirmation_response, confirmation_outcome = await container.orchestrator.handle_text(
        "disable offline mode"
    )

    assert confirmation_response.response_type == ResponseType.CONFIRMATION
    assert confirmation_outcome.confirmation is not None
    assert container.settings.offline_lock is True

    completed_response, completed_outcome = await container.orchestrator.handle_text("yes")

    assert completed_response.severity == "success"
    assert completed_outcome.result.status == ResultStatus.COMPLETED
    assert container.settings.offline_lock is False


@pytest.mark.parametrize(
    ("text", "arguments"),
    [
        (
            "set response detail to detailed",
            {"operation": "set", "setting": "response_detail", "value": "detailed"},
        ),
        (
            "enable high contrast",
            {"operation": "set", "setting": "ui_high_contrast", "value": True},
        ),
        (
            "toggle reduced motion",
            {"operation": "toggle", "setting": "ui_reduced_motion"},
        ),
        (
            "show speech mode",
            {"operation": "get", "setting": "speech_mode"},
        ),
    ],
)
def test_common_assistant_setting_requests_route_without_planner(text, arguments):
    result = DeterministicRouter().route(text)

    assert result.needs_llm is False
    assert result.intent.name == "core.settings"
    assert result.intent.arguments == arguments


def test_internal_settings_are_not_exposed_to_assistant_requests(settings):
    skill = build_container(settings).registry.get("core.settings")

    with pytest.raises(ValueError, match="Unsupported assistant setting"):
        skill.required_permissions(_call("data_dir", "elsewhere", key="internal-setting"))
