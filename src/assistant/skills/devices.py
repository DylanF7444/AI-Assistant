from __future__ import annotations

import asyncio
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from assistant.contracts import (
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.integrations.devices import DeviceCommand, DeviceTransport
from assistant.integrations.home_assistant import HomeAssistantAdapter
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest


class DeviceInput(BaseModel):
    device_id: UUID
    capability: str = Field(pattern=r"^[a-z0-9_.-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class DeviceOutput(BaseModel):
    command_id: UUID
    success: bool
    data: dict[str, Any]


class DeviceControlSkill(Skill):
    input_model = DeviceInput
    output_model = DeviceOutput

    def __init__(self, transports: dict[UUID, DeviceTransport]) -> None:
        self.transports = transports
        self.manifest = manifest(
            skill_id="integration.device_control",
            display_name="Device Control",
            description="Control an explicitly paired external device.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.HIGH,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        target = str(args.device_id)
        return (
            PermissionScope(
                capability="device.control",
                targets=(target,),
                reason="Control this exact paired device.",
            ),
            PermissionScope(
                capability="network",
                targets=(target,),
                reason="Send the authenticated command over the local network.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        return (
            f"Send {args.capability} to paired device {args.device_id} "
            f"with arguments {args.arguments}"
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        transport = self.transports.get(args.device_id)
        if transport is None:
            raise ValueError("Device is not paired or has been revoked")
        command = DeviceCommand(
            device_id=args.device_id,
            capability=args.capability,
            arguments=args.arguments,
        )
        task = asyncio.create_task(transport.send(command))
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({task, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                task.cancel()
                return SkillResult(
                    status=ResultStatus.CANCELLED,
                    summary="Device command cancelled.",
                    data={
                        "command_id": command.command_id,
                        "success": False,
                        "data": {},
                    },
                )
            result = task.result()
        finally:
            cancelled.cancel()
        return SkillResult(
            status=ResultStatus.COMPLETED if result.success else ResultStatus.FAILED,
            summary=result.summary,
            data=DeviceOutput(
                command_id=result.command_id,
                success=result.success,
                data=result.data,
            ).model_dump(mode="json"),
        )


class HomeAssistantInput(BaseModel):
    operation: Literal["state", "call_service"]
    entity_id: str | None = None
    domain: str | None = None
    service: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def required_fields(self):
        if self.operation == "state" and not self.entity_id:
            raise ValueError("state requires entity_id")
        if self.operation == "call_service" and (not self.domain or not self.service):
            raise ValueError("call_service requires domain and service")
        return self


class HomeAssistantOutput(BaseModel):
    result: Any


class HomeAssistantSkill(Skill):
    input_model = HomeAssistantInput
    output_model = HomeAssistantOutput

    def __init__(self, adapter: HomeAssistantAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="integration.home_assistant",
            display_name="Home Assistant",
            description="Read or control an approved Home Assistant entity.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.HIGH,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        target = args.entity_id or f"{args.domain}.{args.service}"
        return (
            PermissionScope(
                capability="device.control" if args.operation == "call_service" else "device.read",
                targets=(target,),
                reason=f"{args.operation.replace('_', ' ').title()} for this exact Home Assistant target.",
            ),
            PermissionScope(
                capability="network",
                targets=(self.adapter.base_url,),
                reason="Connect to the configured Home Assistant server.",
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.HIGH
            if self.validate(call.arguments).operation == "call_service"
            else RiskLevel.MEDIUM
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "state":
            return f"Read Home Assistant state {args.entity_id}"
        return (
            f"Call Home Assistant service {args.domain}.{args.service} "
            f"for {args.entity_id or 'the configured target'}"
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if args.operation == "state":
            value = await self.adapter.state(args.entity_id)
            summary = f"{args.entity_id} is {value.get('state', 'unknown')}."
        else:
            value = await self.adapter.call_service(
                args.domain,
                args.service,
                entity_id=args.entity_id,
                data=args.data,
            )
            summary = f"Home Assistant service {args.domain}.{args.service} completed."
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            data=HomeAssistantOutput(result=value).model_dump(),
            sensitive=args.operation == "state",
        )
