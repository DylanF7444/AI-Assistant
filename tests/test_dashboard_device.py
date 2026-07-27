from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from assistant.bootstrap import build_container
from assistant.dashboard.contracts import (
    DashboardCard,
    DashboardLayout,
    MetricPresentation,
    MetricRequest,
    MetricState,
)
from assistant.dashboard.device import WindowsDeviceMetricProvider
from assistant.dashboard.registry import DashboardProviderRegistry
from assistant.dashboard.service import DashboardService
from assistant.dashboard.storage import DashboardLayoutStore
from assistant.policy.permissions import GrantType
from assistant.storage.database import SettingsStore


@pytest.mark.asyncio
async def test_windows_device_provider_formats_supported_metrics():
    adapter = _FakeDeviceAdapter()
    provider = WindowsDeviceMetricProvider(adapter)

    values = {
        metric_id: await provider.collect(
            MetricRequest(
                metric_id=metric_id,
                since=datetime.now(UTC),
            )
        )
        for metric_id in ("device.cpu", "device.memory", "device.storage", "device.battery")
    }

    assert values["device.cpu"].value == 25
    assert values["device.memory"].value == 60
    assert "8.0 GB available" in values["device.memory"].summary
    assert values["device.storage"].value == 75
    assert values["device.battery"].value == 82
    assert all(value.unit == "%" for value in values.values())


@pytest.mark.asyncio
async def test_device_card_is_not_collected_before_exact_permission(settings):
    container = build_container(settings)
    adapter = _FakeDeviceAdapter()
    provider = WindowsDeviceMetricProvider(adapter)
    registry = DashboardProviderRegistry()
    registry.register(provider)
    card = DashboardCard(
        card_id=uuid4(),
        metric_id="device.cpu",
        presentation=MetricPresentation.PERCENTAGE,
        refresh_seconds=30,
        position=0,
    )
    layout = DashboardLayout(cards=(card,))
    layouts = DashboardLayoutStore(SettingsStore(container.database), lambda: layout)
    layouts.save(layout)
    service = DashboardService(
        registry,
        layouts,
        permissions=container.permissions,
    )

    blocked = await service.refresh()
    assert blocked[card.card_id].state == MetricState.PERMISSION_REQUIRED
    assert adapter.performance_calls == 0

    container.permissions.grant(
        user_id="local-user",
        skill_id=provider.permission_subject,
        capability="device.read",
        grant_type=GrantType.SESSION,
        targets=("wrong-device",),
        session_id="desktop",
    )
    wrong_target = await service.refresh()
    assert wrong_target[card.card_id].state == MetricState.PERMISSION_REQUIRED
    assert adapter.performance_calls == 0

    service.grant_card_permissions(card.card_id, GrantType.SESSION)
    allowed = await service.refresh()
    assert allowed[card.card_id].state == MetricState.READY
    assert allowed[card.card_id].value == 25
    assert adapter.performance_calls == 1

    container.permissions.revoke(
        "local-user",
        provider.permission_subject,
        "device.read",
        "dashboard-test",
    )
    revoked = await service.refresh()
    assert revoked[card.card_id].state == MetricState.PERMISSION_REQUIRED
    assert adapter.performance_calls == 1


class _FakeDeviceAdapter:
    def __init__(self) -> None:
        self.performance_calls = 0

    async def performance_status(self) -> dict:
        self.performance_calls += 1
        return {
            "cpu_percent": 25,
            "memory": {
                "percent_used": 60,
                "available_bytes": 8 * 1024**3,
                "total_bytes": 20 * 1024**3,
            },
        }

    async def storage_status(self, drive: str | None = None) -> list[dict]:
        return [
            {
                "drive": drive or "C:\\",
                "total_bytes": 100 * 1024**3,
                "used_bytes": 75 * 1024**3,
                "free_bytes": 25 * 1024**3,
                "percent_used": 75,
            }
        ]

    async def battery_status(self) -> dict:
        return {
            "percent": 82,
            "plugged_in": True,
            "charging": True,
            "battery_present": True,
            "seconds_remaining": 7200,
        }
