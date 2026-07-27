from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select

from assistant.dashboard.contracts import DashboardLayout, MetricPoint, ProviderConnection
from assistant.storage.database import (
    DashboardMetricSampleRow,
    Database,
    SettingsStore,
)
from assistant.storage.secrets import SecretVault


class DashboardLayoutStore:
    KEY = "dashboard.layout.v1"

    def __init__(
        self,
        settings: SettingsStore,
        default_factory: Callable[[], DashboardLayout],
    ) -> None:
        self.settings = settings
        self.default_factory = default_factory

    def load(self) -> DashboardLayout:
        payload = self.settings.get(self.KEY)
        if payload is None:
            layout = self.default_factory()
            self.save(layout)
            return layout
        try:
            return DashboardLayout.model_validate(payload)
        except (TypeError, ValidationError, ValueError):
            return self.default_factory()

    def save(self, layout: DashboardLayout) -> None:
        validated = DashboardLayout.model_validate(layout)
        self.settings.set(self.KEY, validated.model_dump(mode="json"))

    def default(self) -> DashboardLayout:
        return self.default_factory()

    def reset(self) -> DashboardLayout:
        layout = self.default()
        self.save(layout)
        return layout


class DashboardConnectionStore:
    KEY = "dashboard.connections.v1"

    def __init__(self, settings: SettingsStore, secrets: SecretVault) -> None:
        self.settings = settings
        self.secrets = secrets

    def list(self) -> tuple[ProviderConnection, ...]:
        payload = self.settings.get(self.KEY, [])
        if not isinstance(payload, list):
            return ()
        connections = []
        for value in payload:
            try:
                connections.append(ProviderConnection.model_validate(value))
            except (TypeError, ValidationError, ValueError):
                continue
        return tuple(sorted(connections, key=lambda item: item.display_name.casefold()))

    def get(self, connection_id) -> ProviderConnection:
        try:
            return next(item for item in self.list() if item.connection_id == connection_id)
        except StopIteration as exc:
            raise KeyError(f"Unknown dashboard connection: {connection_id}") from exc

    def save(
        self,
        connection: ProviderConnection,
        *,
        secret_values: dict[str, str] | None = None,
    ) -> ProviderConnection:
        validated = ProviderConnection.model_validate(connection)
        for logical_name, value in (secret_values or {}).items():
            reference = validated.secret_references.get(logical_name)
            if reference is None:
                raise ValueError(f"Missing secret reference for {logical_name}")
            self.secrets.set(reference, value)
        values = [item for item in self.list() if item.connection_id != validated.connection_id]
        values.append(validated)
        self.settings.set(
            self.KEY,
            [
                item.model_dump(mode="json")
                for item in sorted(values, key=lambda value: value.display_name.casefold())
            ],
        )
        return validated

    def delete(self, connection_id) -> ProviderConnection:
        connection = self.get(connection_id)
        values = [item for item in self.list() if item.connection_id != connection.connection_id]
        self.settings.set(
            self.KEY,
            [item.model_dump(mode="json") for item in values],
        )
        for reference in connection.secret_references.values():
            self.secrets.delete(reference)
        return connection


class DashboardSampleStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(
        self,
        *,
        timestamp: datetime,
        connection_id: UUID,
        metric_id: str,
        series_key: str,
        value: float,
        state: str = "ready",
    ) -> None:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("Dashboard history values must be finite")
        with self.database.session() as session:
            session.add(
                DashboardMetricSampleRow(
                    timestamp=timestamp,
                    connection_id=str(connection_id),
                    metric_id=metric_id,
                    series_key=series_key,
                    value=numeric,
                    state=state,
                )
            )

    def history(
        self,
        *,
        series_key: str,
        since: datetime,
        limit: int = 100,
    ) -> tuple[MetricPoint, ...]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(DashboardMetricSampleRow)
                    .where(
                        DashboardMetricSampleRow.series_key == series_key,
                        DashboardMetricSampleRow.timestamp >= since,
                    )
                    .order_by(DashboardMetricSampleRow.id.desc())
                    .limit(max(1, min(limit, 1000)))
                )
            )
        return tuple(
            MetricPoint(timestamp=row.timestamp, value=row.value) for row in reversed(rows)
        )

    def clear_connection(self, connection_id: UUID) -> int:
        with self.database.session() as session:
            return int(
                session.query(DashboardMetricSampleRow)
                .filter(DashboardMetricSampleRow.connection_id == str(connection_id))
                .delete()
            )

    def prune(self, before: datetime) -> int:
        with self.database.session() as session:
            return int(
                session.query(DashboardMetricSampleRow)
                .filter(DashboardMetricSampleRow.timestamp < before)
                .delete()
            )

    def count(self) -> int:
        with self.database.session() as session:
            return int(session.query(DashboardMetricSampleRow).count())
