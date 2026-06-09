from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from assistant.contracts import PermissionContext, ResultStatus, RiskLevel, SkillCall
from assistant.integrations.devices import DeviceHealth, DeviceResult
from assistant.skills.devices import DeviceControlSkill, HomeAssistantSkill


class FakeTransport:
    async def send(self, command):
        return DeviceResult(
            command_id=command.command_id,
            success=True,
            summary="Light turned on.",
            data={"state": "on"},
        )

    async def health(self, device_id):
        return DeviceHealth(device_id=device_id, online=True)


class FakeHomeAssistant:
    base_url = "https://home.local"

    async def state(self, entity_id):
        return {"entity_id": entity_id, "state": "on"}

    async def call_service(self, domain, service, entity_id=None, data=None):
        return [{"domain": domain, "service": service, "entity_id": entity_id}]


def call(skill_id, arguments):
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


@pytest.mark.asyncio
async def test_paired_device_control_is_target_scoped():
    device_id = uuid4()
    skill = DeviceControlSkill({device_id: FakeTransport()})
    skill_call = call(
        "integration.device_control",
        {
            "device_id": str(device_id),
            "capability": "light.on",
            "arguments": {},
        },
    )
    scopes = skill.required_permissions(skill_call)
    assert all(str(device_id) in scope.targets for scope in scopes)
    result = await skill.execute(skill_call, asyncio.Event())
    assert result.status == ResultStatus.COMPLETED


@pytest.mark.asyncio
async def test_home_assistant_reads_and_actions_have_distinct_risk():
    skill = HomeAssistantSkill(FakeHomeAssistant())
    state_call = call(
        "integration.home_assistant",
        {"operation": "state", "entity_id": "light.kitchen"},
    )
    assert skill.risk_for(state_call) == RiskLevel.MEDIUM
    state = await skill.execute(state_call, asyncio.Event())
    assert state.data["result"]["state"] == "on"
    action_call = call(
        "integration.home_assistant",
        {
            "operation": "call_service",
            "domain": "light",
            "service": "turn_off",
            "entity_id": "light.kitchen",
        },
    )
    assert skill.risk_for(action_call) == RiskLevel.HIGH
