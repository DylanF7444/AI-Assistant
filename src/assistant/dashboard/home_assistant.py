from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

from assistant.contracts import PermissionScope
from assistant.dashboard.contracts import (
    DashboardCard,
    MetricDefinition,
    MetricPresentation,
    MetricRequest,
    MetricSnapshot,
    MetricState,
    MetricValueKind,
    ProviderConnection,
    ProviderHealth,
)
from assistant.integrations.home_assistant import HomeAssistantAdapter
from assistant.storage.secrets import SecretVault

_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_ATTRIBUTE = re.compile(r"^[A-Za-z0-9_]+$")


class HomeAssistantMetricProvider:
    provider_id = "home_assistant"
    permission_subject = "dashboard.home_assistant"
    remote = True

    def __init__(
        self,
        secrets: SecretVault,
        *,
        adapter_factory: Callable[[str, str], HomeAssistantAdapter] | None = None,
    ) -> None:
        self.secrets = secrets
        self.adapter_factory = adapter_factory or HomeAssistantAdapter

    def definitions(self) -> tuple[MetricDefinition, ...]:
        return (
            MetricDefinition(
                metric_id="home_assistant.entity_state",
                provider_id=self.provider_id,
                display_name="Home Assistant entity",
                description=("Display one exact Home Assistant entity state or scalar attribute."),
                source_label="Home Assistant",
                value_kind=MetricValueKind.STATUS,
                supported_presentations=(
                    MetricPresentation.STATUS,
                    MetricPresentation.NUMBER,
                    MetricPresentation.PERCENTAGE,
                    MetricPresentation.PROGRESS,
                    MetricPresentation.TREND,
                ),
                default_presentation=MetricPresentation.STATUS,
                default_refresh_seconds=300,
                minimum_refresh_seconds=60,
                supports_history=True,
                sensitive=True,
                requires_connection=True,
            ),
        )

    def validate_connection(self, connection: ProviderConnection) -> None:
        if connection.provider_id != self.provider_id:
            raise ValueError("Connection does not belong to Home Assistant")
        if not connection.endpoint:
            raise ValueError("Home Assistant base URL is required")
        if "token" not in connection.secret_references:
            raise ValueError("Home Assistant token reference is required")
        parsed = urlparse(connection.endpoint)
        if parsed.username or parsed.password:
            raise ValueError("Home Assistant URL cannot contain credentials")
        HomeAssistantAdapter(connection.endpoint, "validation-only")

    def validate_card(
        self,
        card: DashboardCard,
        connection: ProviderConnection | None,
    ) -> None:
        if connection is None:
            raise ValueError("Select a Home Assistant connection")
        self.validate_connection(connection)
        entity_id = str(card.options.get("entity_id", "")).strip()
        if not _ENTITY_ID.fullmatch(entity_id):
            raise ValueError("Home Assistant entity ID must look like sensor.temperature")
        attribute = str(card.options.get("attribute", "")).strip()
        if attribute and not _ATTRIBUTE.fullmatch(attribute):
            raise ValueError("Home Assistant attribute must be one field name")

    def required_capabilities(
        self,
        _definition: MetricDefinition,
        connection: ProviderConnection | None,
        options: dict,
    ) -> tuple[PermissionScope, ...]:
        if connection is None or not connection.endpoint:
            raise ValueError("Home Assistant connection is required")
        entity_id = str(options.get("entity_id", "")).strip()
        attribute = str(options.get("attribute", "")).strip() or "state"
        parsed = urlparse(connection.endpoint)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return (
            PermissionScope(
                capability="integration.read",
                targets=(f"home-assistant:{connection.connection_id}:{entity_id}:{attribute}",),
                reason="Read the exact configured Home Assistant entity metric.",
            ),
            PermissionScope(
                capability="network",
                targets=(origin,),
                reason="Connect to the configured Home Assistant origin.",
            ),
        )

    def permission_subject_for(self, connection: ProviderConnection | None) -> str:
        if connection is None:
            return self.permission_subject
        return f"{self.permission_subject}.{connection.connection_id}"

    async def health(self, connection: ProviderConnection | None) -> ProviderHealth:
        checked_at = datetime.now(UTC)
        try:
            adapter = self._adapter(connection)
            health = await adapter.health()
            if health.get("healthy"):
                return ProviderHealth(
                    healthy=True,
                    checked_at=checked_at,
                    message="Home Assistant is reachable.",
                )
            return ProviderHealth(
                healthy=False,
                checked_at=checked_at,
                message="Home Assistant did not report healthy.",
                error_code="unhealthy",
            )
        except Exception as error:
            return ProviderHealth(
                healthy=False,
                checked_at=checked_at,
                message="Home Assistant connection failed.",
                error_code=type(error).__name__.lower(),
            )

    async def collect(
        self,
        request: MetricRequest,
        connection: ProviderConnection | None,
    ) -> MetricSnapshot:
        if request.metric_id != "home_assistant.entity_state":
            raise KeyError(request.metric_id)
        if connection is None or not connection.enabled:
            raise ValueError("Home Assistant connection is disabled or missing")
        entity_id = str(request.options.get("entity_id", "")).strip()
        attribute = str(request.options.get("attribute", "")).strip()
        self.validate_card(
            DashboardCard(
                card_id=uuid4(),
                metric_id=request.metric_id,
                connection_id=connection.connection_id,
                presentation=MetricPresentation.STATUS,
                refresh_seconds=300,
                position=0,
                options={"entity_id": entity_id, "attribute": attribute},
            ),
            connection,
        )
        payload = await self._adapter(connection).state(entity_id)
        attributes = payload.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        raw_value = attributes.get(attribute) if attribute else payload.get("state")
        if raw_value is None:
            return MetricSnapshot(
                metric_id=request.metric_id,
                connection_id=connection.connection_id,
                collected_at=datetime.now(UTC),
                state=MetricState.EMPTY,
                summary="The configured entity field has no value.",
            )
        value = _scalar_value(raw_value)
        unit = str(attributes.get("unit_of_measurement", "")).strip() or None
        friendly_name = str(attributes.get("friendly_name", entity_id)).strip()[:160]
        return MetricSnapshot(
            metric_id=request.metric_id,
            connection_id=connection.connection_id,
            collected_at=datetime.now(UTC),
            state=MetricState.READY,
            value=value,
            unit=unit,
            summary=(f"{friendly_name}  •  {attribute or 'state'}  •  {connection.display_name}"),
        )

    def _adapter(self, connection: ProviderConnection | None) -> HomeAssistantAdapter:
        if connection is None:
            raise ValueError("Home Assistant connection is required")
        self.validate_connection(connection)
        reference = connection.secret_references["token"]
        token = self.secrets.get(reference)
        if token is None:
            raise ValueError("Home Assistant credential is unavailable")
        assert connection.endpoint is not None
        return self.adapter_factory(connection.endpoint, token)


def _scalar_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        raise ValueError("Home Assistant dashboard values must be scalar")
    normalized = str(value).strip()
    try:
        return float(normalized)
    except ValueError:
        return normalized[:160]
