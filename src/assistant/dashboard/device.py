from __future__ import annotations

import re
from datetime import UTC, datetime

from assistant.contracts import PermissionScope
from assistant.dashboard.contracts import (
    DashboardCard,
    MetricDefinition,
    MetricDetail,
    MetricPresentation,
    MetricRequest,
    MetricSnapshot,
    MetricState,
    MetricValueKind,
    ProviderConnection,
    ProviderHealth,
)
from assistant.windows.interfaces import WindowsDeviceAdapter

_LOCAL_DEVICE_SCOPE = (
    PermissionScope(
        capability="device.read",
        targets=("local-device",),
        reason="Display read-only performance and health metrics for this Windows device.",
    ),
)


class WindowsDeviceMetricProvider:
    provider_id = "assistant.windows_device"
    permission_subject = "dashboard.windows_device"
    remote = False

    def __init__(self, adapter: WindowsDeviceAdapter) -> None:
        self.adapter = adapter

    def definitions(self) -> tuple[MetricDefinition, ...]:
        return (
            self._percentage_definition(
                "device.cpu",
                "CPU usage",
                "Current total processor utilization.",
            ),
            self._percentage_definition(
                "device.memory",
                "Memory usage",
                "Current physical memory utilization.",
            ),
            self._percentage_definition(
                "device.storage",
                "Storage usage",
                "Used space on the selected local drive.",
                default_refresh_seconds=60,
            ),
            self._percentage_definition(
                "device.battery",
                "Battery",
                "Current battery charge and power state.",
                default_refresh_seconds=60,
            ),
        )

    def validate_card(
        self,
        _card: DashboardCard,
        connection: ProviderConnection | None,
    ) -> None:
        if connection is not None:
            raise ValueError("Windows device metrics do not use a connection")

    @staticmethod
    def required_capabilities(
        definition: MetricDefinition,
        _connection: ProviderConnection | None,
        _options: dict,
    ):
        return definition.required_capabilities

    def permission_subject_for(self, _connection: ProviderConnection | None) -> str:
        return self.permission_subject

    async def health(self, _connection: ProviderConnection | None = None) -> ProviderHealth:
        return ProviderHealth(
            healthy=True,
            checked_at=datetime.now(UTC),
            message="Windows device adapter is available.",
        )

    async def collect(
        self,
        request: MetricRequest,
        _connection: ProviderConnection | None = None,
    ) -> MetricSnapshot:
        if request.metric_id == "device.cpu":
            performance = await self.adapter.performance_status()
            value = float(performance.get("cpu_percent", 0))
            return self._percentage_snapshot(
                request.metric_id,
                value,
                "Current processor utilization.",
            )
        if request.metric_id == "device.memory":
            performance = await self.adapter.performance_status()
            memory = performance.get("memory") or {}
            value = float(memory.get("percent_used", 0))
            return self._percentage_snapshot(
                request.metric_id,
                value,
                (
                    f"{_bytes(memory.get('available_bytes'))} available of "
                    f"{_bytes(memory.get('total_bytes'))}"
                ),
            )
        if request.metric_id == "device.storage":
            drive = _validated_drive(request.options.get("drive"))
            rows = await self.adapter.storage_status(drive)
            if not rows:
                return self._empty(request.metric_id, "No matching local drive was found.")
            row = rows[0]
            return self._percentage_snapshot(
                request.metric_id,
                float(row.get("percent_used", 0)),
                f"{_bytes(row.get('free_bytes'))} free on {row.get('drive') or 'local drive'}",
                details=(
                    MetricDetail(label="Used", value=_bytes(row.get("used_bytes"))),
                    MetricDetail(label="Total", value=_bytes(row.get("total_bytes"))),
                ),
            )
        if request.metric_id == "device.battery":
            battery = await self.adapter.battery_status()
            if not battery.get("battery_present", True) or battery.get("percent") is None:
                return self._empty(request.metric_id, "No battery is present.")
            states = []
            if battery.get("charging"):
                states.append("Charging")
            if battery.get("plugged_in"):
                states.append("Plugged in")
            if not states:
                states.append("On battery")
            details = ()
            if battery.get("seconds_remaining") is not None:
                details = (
                    MetricDetail(
                        label="Estimated remaining",
                        value=_duration(int(battery["seconds_remaining"])),
                    ),
                )
            return self._percentage_snapshot(
                request.metric_id,
                float(battery["percent"]),
                "  •  ".join(states),
                details=details,
            )
        raise KeyError(request.metric_id)

    def _percentage_definition(
        self,
        metric_id: str,
        display_name: str,
        description: str,
        *,
        default_refresh_seconds: int = 30,
    ) -> MetricDefinition:
        return MetricDefinition(
            metric_id=metric_id,
            provider_id=self.provider_id,
            display_name=display_name,
            description=description,
            source_label="Windows device",
            value_kind=MetricValueKind.PERCENTAGE,
            unit="%",
            supported_presentations=(
                MetricPresentation.PERCENTAGE,
                MetricPresentation.PROGRESS,
                MetricPresentation.NUMBER,
            ),
            default_presentation=MetricPresentation.PERCENTAGE,
            required_capabilities=_LOCAL_DEVICE_SCOPE,
            default_refresh_seconds=default_refresh_seconds,
            minimum_refresh_seconds=15,
        )

    @staticmethod
    def _percentage_snapshot(
        metric_id: str,
        value: float,
        summary: str,
        *,
        details: tuple[MetricDetail, ...] = (),
    ) -> MetricSnapshot:
        return MetricSnapshot(
            metric_id=metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=max(0.0, min(100.0, value)),
            unit="%",
            summary=summary,
            details=details,
        )

    @staticmethod
    def _empty(metric_id: str, summary: str) -> MetricSnapshot:
        return MetricSnapshot(
            metric_id=metric_id,
            collected_at=datetime.now(UTC),
            state=MetricState.EMPTY,
            summary=summary,
        )


def _validated_drive(value: object) -> str | None:
    if value in (None, ""):
        return None
    drive = str(value).strip()
    if not re.fullmatch(r"[A-Za-z]:\\?", drive):
        raise ValueError("Drive must be a local drive letter")
    return f"{drive[0].upper()}:"


def _bytes(value: object) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return "Unknown"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return "Unknown"


def _duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
