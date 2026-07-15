from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from assistant.contracts import (
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.windows.interfaces import (
    WindowsDeviceAdapter,
)


class DeviceControlInput(BaseModel):
    operation: Literal[
        "search_installed_apps",
        "set_volume",
        "change_volume",
        "mute_audio",
        "unmute_audio",
        "set_brightness",
        "change_brightness",
        "toggle_wifi",
        "connect_wifi_network",
        "disconnect_wifi_network",
        "toggle_bluetooth",
        "connect_bluetooth_device",
        "disconnect_bluetooth_device",
        "lock_device",
        "restart_device",
        "shutdown_device",
        "battery_status",
        "storage_status",
        "performance_status",
        "connected_devices",
        "enable_connected_device",
        "disable_connected_device",
    ]
    query: str | None = Field(default=None, max_length=200)
    level: int | None = Field(default=None, ge=0, le=100)
    delta: int | None = Field(default=None, ge=-100, le=100)
    enabled: bool | None = None
    network_name: str | None = Field(default=None, min_length=1, max_length=100)
    device_name: str | None = Field(default=None, min_length=1, max_length=200)
    drive: str | None = Field(default=None, pattern=r"^[A-Za-z]:?\\?$")
    instance_id: str | None = Field(default=None, min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=200)

    @model_validator(mode="after")
    def operation_arguments(self):
        if self.operation in {"set_volume", "set_brightness"} and self.level is None:
            raise ValueError(f"{self.operation} requires level")
        if self.operation in {"change_volume", "change_brightness"} and not self.delta:
            raise ValueError(f"{self.operation} requires a non-zero delta")
        if (
            self.operation in {"enable_connected_device", "disable_connected_device"}
            and not self.instance_id
        ):
            raise ValueError(f"{self.operation} requires instance_id")
        if self.operation == "connect_wifi_network" and not self.network_name:
            raise ValueError("connect_wifi_network requires network_name")
        if (
            self.operation in {"connect_bluetooth_device", "disconnect_bluetooth_device"}
            and not self.device_name
        ):
            raise ValueError(f"{self.operation} requires device_name")
        return self


class DeviceControlOutput(BaseModel):
    operation: str
    result: object


class WindowsDeviceControlSkill(Skill):
    input_model = DeviceControlInput
    output_model = DeviceControlOutput

    READ_OPERATIONS = {
        "battery_status",
        "storage_status",
        "performance_status",
        "connected_devices",
    }
    AUDIO_OPERATIONS = {"set_volume", "change_volume", "mute_audio", "unmute_audio"}
    DISPLAY_OPERATIONS = {"set_brightness", "change_brightness"}
    NETWORK_OPERATIONS = {
        "toggle_wifi",
        "connect_wifi_network",
        "disconnect_wifi_network",
        "toggle_bluetooth",
        "connect_bluetooth_device",
        "disconnect_bluetooth_device",
    }
    POWER_OPERATIONS = {"lock_device", "restart_device", "shutdown_device"}

    def __init__(self, adapter: WindowsDeviceAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="windows.device_control",
            display_name="Windows Device Control",
            description=(
                "Control local Windows device settings and read CPU, memory, battery, "
                "storage, performance, and connected-device status."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(),
            risk=RiskLevel.CRITICAL,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        capability = self._capability(args)
        target = self._target(args)
        return (
            PermissionScope(
                capability=capability,
                targets=(target,),
                reason=f"{args.operation.replace('_', ' ').title()} on this Windows device.",
            ),
        )

    def risk_for(self, call: SkillCall) -> RiskLevel:
        operation = self.validate(call.arguments).operation
        if operation in {"restart_device", "shutdown_device"}:
            return RiskLevel.CRITICAL
        if operation == "lock_device":
            return RiskLevel.HIGH
        if operation in {"enable_connected_device", "disable_connected_device"}:
            return RiskLevel.HIGH
        if operation in self.READ_OPERATIONS or operation == "search_installed_apps":
            return RiskLevel.LOW
        return RiskLevel.MEDIUM

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        name = args.operation.replace("_", " ")
        if args.operation in {"set_volume", "set_brightness"}:
            return f"{name.title()} to {args.level}%"
        if args.operation in {"change_volume", "change_brightness"}:
            return f"{name.title()} by {args.delta}%"
        if args.operation in {"toggle_wifi", "toggle_bluetooth"}:
            state = "toggle" if args.enabled is None else ("on" if args.enabled else "off")
            return f"{name.title()} {state}"
        if args.operation in {"connect_wifi_network", "disconnect_wifi_network"}:
            target = args.network_name or "current network"
            return f"{name.title()} {target}"
        if args.operation in {"connect_bluetooth_device", "disconnect_bluetooth_device"}:
            return f"{name.title()} {args.device_name}"
        if args.operation in {"enable_connected_device", "disable_connected_device"}:
            return f"{name.title()} {args.instance_id}"
        return name.title()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        result: object
        if args.operation == "search_installed_apps":
            result = await self.adapter.search_installed_apps(args.query, args.limit)
            summary = f"Found {len(result)} installed application{'s' if len(result) != 1 else ''}."
        elif args.operation == "set_volume":
            await self.adapter.set_volume(args.level or 0)
            result = {"level": args.level}
            summary = f"Volume set to {args.level}%."
        elif args.operation == "change_volume":
            await self.adapter.change_volume(args.delta or 0)
            result = {"delta": args.delta}
            summary = "Volume adjusted."
        elif args.operation == "mute_audio":
            await self.adapter.mute_audio(True)
            result = {"muted": True}
            summary = "Audio muted."
        elif args.operation == "unmute_audio":
            await self.adapter.mute_audio(False)
            result = {"muted": False}
            summary = "Audio unmuted."
        elif args.operation == "set_brightness":
            await self.adapter.set_brightness(args.level or 0)
            result = {"level": args.level}
            summary = f"Brightness set to {args.level}%."
        elif args.operation == "change_brightness":
            level = await self.adapter.change_brightness(args.delta or 0)
            result = {"level": level, "delta": args.delta}
            summary = f"Brightness adjusted to {level}%."
        elif args.operation == "toggle_wifi":
            enabled = await self.adapter.set_wifi_enabled(args.enabled)
            result = {"enabled": enabled}
            summary = f"Wi-Fi turned {'on' if enabled else 'off'}."
        elif args.operation == "connect_wifi_network":
            await self.adapter.connect_wifi_network(args.network_name or "")
            result = {"network_name": args.network_name, "connected": True}
            summary = f"Connected to Wi-Fi network {args.network_name}."
        elif args.operation == "disconnect_wifi_network":
            await self.adapter.disconnect_wifi_network(args.network_name)
            result = {"network_name": args.network_name, "connected": False}
            summary = (
                f"Disconnected from Wi-Fi network {args.network_name}."
                if args.network_name
                else "Disconnected from Wi-Fi."
            )
        elif args.operation == "toggle_bluetooth":
            enabled = await self.adapter.set_bluetooth_enabled(args.enabled)
            result = {"enabled": enabled}
            summary = f"Bluetooth turned {'on' if enabled else 'off'}."
        elif args.operation == "connect_bluetooth_device":
            await self.adapter.connect_bluetooth_device(args.device_name or "")
            result = {"device_name": args.device_name, "connected": True}
            summary = f"Connected Bluetooth device {args.device_name}."
        elif args.operation == "disconnect_bluetooth_device":
            await self.adapter.disconnect_bluetooth_device(args.device_name or "")
            result = {"device_name": args.device_name, "connected": False}
            summary = f"Disconnected Bluetooth device {args.device_name}."
        elif args.operation == "lock_device":
            await self.adapter.lock_device()
            result = {"locked": True}
            summary = "Device locked."
        elif args.operation == "restart_device":
            await self.adapter.restart_device()
            result = {"restart_requested": True}
            summary = "Restart requested."
        elif args.operation == "shutdown_device":
            await self.adapter.shutdown_device()
            result = {"shutdown_requested": True}
            summary = "Shutdown requested."
        elif args.operation == "battery_status":
            result = await self.adapter.battery_status()
            summary = "Battery status retrieved."
        elif args.operation == "storage_status":
            result = await self.adapter.storage_status(args.drive)
            summary = "Storage status retrieved."
        elif args.operation == "performance_status":
            result = await self.adapter.performance_status()
            cpu_percent = result.get("cpu_percent") if isinstance(result, dict) else None
            memory = result.get("memory") if isinstance(result, dict) else None
            memory_percent = memory.get("percent_used") if isinstance(memory, dict) else None
            if isinstance(cpu_percent, (int, float)) and isinstance(memory_percent, (int, float)):
                summary = f"CPU usage: {cpu_percent:g}%. Memory usage: {memory_percent:g}%."
            else:
                summary = "CPU and memory usage retrieved."
        elif args.operation == "connected_devices":
            result = await self.adapter.connected_devices(args.limit)
            summary = f"Found {len(result)} connected device{'s' if len(result) != 1 else ''}."
        elif args.operation == "enable_connected_device":
            await self.adapter.set_connected_device_enabled(args.instance_id or "", True)
            result = {"instance_id": args.instance_id, "enabled": True}
            summary = "Connected device enabled."
        elif args.operation == "disable_connected_device":
            await self.adapter.set_connected_device_enabled(args.instance_id or "", False)
            result = {"instance_id": args.instance_id, "enabled": False}
            summary = "Connected device disabled."
        else:
            raise ValueError(f"Unsupported device operation: {args.operation}")
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            data=DeviceControlOutput(operation=args.operation, result=result).model_dump(),
            sensitive=args.operation in self.READ_OPERATIONS
            or args.operation == "search_installed_apps",
        )

    @classmethod
    def _capability(cls, args: DeviceControlInput) -> str:
        if args.operation == "search_installed_apps":
            return "system.apps"
        if args.operation in cls.AUDIO_OPERATIONS:
            return "media.control"
        if args.operation in cls.DISPLAY_OPERATIONS:
            return "system.display"
        if args.operation in cls.NETWORK_OPERATIONS:
            return "system.network"
        if args.operation in cls.POWER_OPERATIONS:
            return "system.power"
        if args.operation in {"enable_connected_device", "disable_connected_device"}:
            return "device.control"
        return "device.read"

    @staticmethod
    def _target(args: DeviceControlInput) -> str:
        if args.operation == "storage_status" and args.drive:
            return args.drive.upper().rstrip("\\")
        if args.operation == "search_installed_apps":
            return args.query or "*"
        if args.operation == "toggle_wifi":
            return "wi-fi"
        if args.operation in {"connect_wifi_network", "disconnect_wifi_network"}:
            return args.network_name or "wi-fi"
        if args.operation == "toggle_bluetooth":
            return "bluetooth"
        if args.operation in {"connect_bluetooth_device", "disconnect_bluetooth_device"}:
            return args.device_name or "bluetooth"
        if args.operation in {"set_volume", "change_volume", "mute_audio", "unmute_audio"}:
            return "system-audio"
        if args.operation in {"set_brightness", "change_brightness"}:
            return "screen-brightness"
        if args.operation in {"enable_connected_device", "disable_connected_device"}:
            return args.instance_id or "*"
        return args.operation
