from __future__ import annotations

from uuid import uuid4

import pytest

from assistant.bootstrap import build_container
from assistant.dashboard.contracts import (
    DashboardCard,
    DashboardLayout,
    MetricPresentation,
    MetricState,
)
from assistant.dashboard.home_assistant import HomeAssistantMetricProvider
from assistant.dashboard.registry import DashboardProviderRegistry
from assistant.dashboard.service import DashboardService
from assistant.dashboard.storage import DashboardConnectionStore, DashboardLayoutStore
from assistant.policy.permissions import GrantType
from assistant.storage.database import SettingsStore
from assistant.storage.secrets import SecretVault


@pytest.mark.asyncio
async def test_home_assistant_connection_is_secret_safe_scoped_and_offline_aware(
    settings, tmp_path
):
    container = build_container(settings)
    cipher = _FakeCipher()
    vault = SecretVault(tmp_path / "dashboard-secrets", cipher=cipher)
    adapters = []

    def adapter_factory(base_url: str, token: str):
        adapter = _FakeHomeAssistantAdapter(base_url, token)
        adapters.append(adapter)
        return adapter

    provider = HomeAssistantMetricProvider(vault, adapter_factory=adapter_factory)
    registry = DashboardProviderRegistry()
    registry.register(provider)
    values = SettingsStore(container.database)
    initial_layout = DashboardLayout(cards=())
    layouts = DashboardLayoutStore(values, lambda: initial_layout)
    layouts.save(initial_layout)
    connections = DashboardConnectionStore(values, vault)
    service = DashboardService(
        registry,
        layouts,
        permissions=container.permissions,
        connections=connections,
        audit=container.audit,
    )

    connection = await service.create_connection(
        provider_id="home_assistant",
        display_name="My Home",
        endpoint="https://home.example.com",
        secret_values={"token": "top-secret-token"},
    )
    token_reference = connection.secret_references["token"]
    assert vault.has(token_reference)
    assert (
        b"top-secret-token"
        not in (tmp_path / "dashboard-secrets" / f"{token_reference}.bin").read_bytes()
    )
    assert "top-secret-token" not in str(values.get(DashboardConnectionStore.KEY))
    assert adapters[0].health_calls == 1

    card = DashboardCard(
        card_id=uuid4(),
        metric_id="home_assistant.entity_state",
        connection_id=connection.connection_id,
        presentation=MetricPresentation.NUMBER,
        refresh_seconds=300,
        position=0,
        options={"entity_id": "sensor.office_temperature", "attribute": ""},
    )
    service.save_layout(DashboardLayout(cards=(card,)))

    blocked = await service.refresh()
    assert blocked[card.card_id].state == MetricState.PERMISSION_REQUIRED
    assert sum(adapter.state_calls for adapter in adapters) == 0
    permission = service.permission_request(card.card_id)
    assert {scope.capability for scope in permission.scopes} == {
        "integration.read",
        "network",
    }
    assert permission.scopes[0].targets == (
        f"home-assistant:{connection.connection_id}:sensor.office_temperature:state",
    )
    assert permission.scopes[1].targets == ("https://home.example.com",)

    service.grant_card_permissions(card.card_id, GrantType.ALWAYS)
    refreshed = await service.refresh()
    assert refreshed[card.card_id].state == MetricState.READY
    assert refreshed[card.card_id].value == 72.5
    assert refreshed[card.card_id].unit == "°F"
    assert sum(adapter.state_calls for adapter in adapters) == 1

    service.set_offline_lock(True)
    offline = await service.refresh()
    assert offline[card.card_id].state == MetricState.OFFLINE
    assert offline[card.card_id].value == 72.5
    assert sum(adapter.state_calls for adapter in adapters) == 1
    await service.refresh()
    blocked_audits = [
        row for row in container.audit.query() if row["event_type"] == "dashboard_refresh_blocked"
    ]
    assert len(blocked_audits) == 1

    service.set_offline_lock(False)
    deleted = service.delete_connection(connection.connection_id)
    assert deleted.connection_id == connection.connection_id
    assert service.connections() == ()
    assert service.layout().cards == ()
    assert not vault.has(token_reference)
    subject = provider.permission_subject_for(connection)
    assert all(grant["skill_id"] != subject for grant in container.permissions.list_grants())
    assert all("top-secret-token" not in str(row) for row in container.audit.query())


@pytest.mark.asyncio
async def test_failed_home_assistant_health_check_rolls_back_secret_and_connection(
    settings, tmp_path
):
    container = build_container(settings)
    vault = SecretVault(tmp_path / "dashboard-secrets", cipher=_FakeCipher())
    provider = HomeAssistantMetricProvider(
        vault,
        adapter_factory=lambda base_url, token: _FakeHomeAssistantAdapter(
            base_url,
            token,
            healthy=False,
        ),
    )
    registry = DashboardProviderRegistry()
    registry.register(provider)
    values = SettingsStore(container.database)
    connections = DashboardConnectionStore(values, vault)
    service = DashboardService(
        registry,
        DashboardLayoutStore(values, lambda: DashboardLayout(cards=())),
        permissions=container.permissions,
        connections=connections,
    )

    with pytest.raises(ConnectionError, match="did not report healthy"):
        await service.create_connection(
            provider_id="home_assistant",
            display_name="Unavailable Home",
            endpoint="https://home.example.com",
            secret_values={"token": "rollback-token"},
        )

    assert connections.list() == ()
    assert list((tmp_path / "dashboard-secrets").glob("*.bin")) == []


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://home.example.com",
        "ftp://home.example.com",
        "https://user:password@home.example.com",
    ),
)
@pytest.mark.asyncio
async def test_home_assistant_rejects_unsafe_remote_endpoints(settings, tmp_path, endpoint):
    container = build_container(settings)
    vault = SecretVault(tmp_path / "dashboard-secrets", cipher=_FakeCipher())
    provider = HomeAssistantMetricProvider(vault)
    registry = DashboardProviderRegistry()
    registry.register(provider)
    values = SettingsStore(container.database)
    service = DashboardService(
        registry,
        DashboardLayoutStore(values, lambda: DashboardLayout(cards=())),
        connections=DashboardConnectionStore(values, vault),
        permissions=container.permissions,
    )

    with pytest.raises(ValueError):
        await service.create_connection(
            provider_id="home_assistant",
            display_name="Unsafe",
            endpoint=endpoint,
            secret_values={"token": "token"},
        )


@pytest.mark.asyncio
async def test_home_assistant_allows_plain_http_only_for_localhost(settings, tmp_path):
    container = build_container(settings)
    vault = SecretVault(tmp_path / "dashboard-secrets", cipher=_FakeCipher())
    provider = HomeAssistantMetricProvider(
        vault,
        adapter_factory=_FakeHomeAssistantAdapter,
    )
    registry = DashboardProviderRegistry()
    registry.register(provider)
    values = SettingsStore(container.database)
    service = DashboardService(
        registry,
        DashboardLayoutStore(values, lambda: DashboardLayout(cards=())),
        connections=DashboardConnectionStore(values, vault),
        permissions=container.permissions,
    )

    connection = await service.create_connection(
        provider_id="home_assistant",
        display_name="Local Home",
        endpoint="http://localhost:8123",
        secret_values={"token": "token"},
    )

    assert connection.endpoint == "http://localhost:8123"


class _FakeCipher:
    def encrypt(self, plaintext: str) -> bytes:
        return plaintext[::-1].encode("utf-8")

    def decrypt(self, ciphertext: bytes) -> str:
        return ciphertext.decode("utf-8")[::-1]


class _FakeHomeAssistantAdapter:
    def __init__(self, base_url: str, token: str, *, healthy: bool = True) -> None:
        self.base_url = base_url
        self.token = token
        self.healthy = healthy
        self.health_calls = 0
        self.state_calls = 0

    async def health(self) -> dict:
        self.health_calls += 1
        return {"healthy": self.healthy}

    async def state(self, entity_id: str) -> dict:
        self.state_calls += 1
        return {
            "entity_id": entity_id,
            "state": "72.5",
            "attributes": {
                "friendly_name": "Office temperature",
                "unit_of_measurement": "°F",
            },
        }
