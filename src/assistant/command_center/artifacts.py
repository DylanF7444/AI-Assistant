from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select

from assistant.command_center.contracts import (
    ArtifactSourceReference,
    DeliveryState,
    DestinationBinding,
    PendingDelivery,
    WorkArtifact,
    WorkBrief,
)
from assistant.contracts import ResultStatus
from assistant.storage.database import (
    CommandCenterArtifactRevisionRow,
    CommandCenterArtifactRow,
    CommandCenterDeliveryRow,
    Database,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def artifact_digest(
    title: str,
    body: str,
    sources: tuple[ArtifactSourceReference, ...],
) -> str:
    return _digest(
        {
            "title": title,
            "body": body,
            "sources": [source.model_dump(mode="json") for source in sources],
        }
    )


def destination_digest(binding: DestinationBinding) -> str:
    return _digest(binding.model_dump(mode="json"))


def source_binding_digest(binding) -> str:
    return _digest(binding.model_dump(mode="json"))


class CommandCenterArtifactStore:
    """Durable, revisioned work artifacts and reviewed delivery records."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def save_brief(
        self,
        brief: WorkBrief,
        *,
        retention_days: int = 30,
        origin_run_id: UUID | None = None,
    ) -> WorkArtifact:
        sources = tuple(
            ArtifactSourceReference(
                binding_id=source.binding_id,
                source_id=source.source_id,
                provider_id=source.provider_id,
                display_name=source.display_name,
                collected_at=source.collected_at,
            )
            for source in brief.sources
        )
        return self.create(
            title=brief.title,
            body=brief.content,
            sources=sources,
            sensitive=brief.sensitive,
            retention_days=retention_days,
            origin_run_id=origin_run_id,
        )

    def create(
        self,
        *,
        title: str,
        body: str,
        sources: tuple[ArtifactSourceReference, ...] = (),
        sensitive: bool = False,
        retention_days: int = 30,
        origin_run_id: UUID | None = None,
    ) -> WorkArtifact:
        if not 1 <= retention_days <= 365:
            raise ValueError("Artifact retention must be between 1 and 365 days")
        if origin_run_id is not None:
            existing = self.find_by_origin_run(origin_run_id)
            if existing is not None:
                return existing
        now = datetime.now(UTC)
        artifact_id = uuid4()
        expires_at = now + timedelta(days=retention_days)
        digest = artifact_digest(title, body, sources)
        artifact = WorkArtifact(
            artifact_id=artifact_id,
            title=title,
            body=body,
            digest=digest,
            sources=sources,
            sensitive=sensitive,
            origin_run_id=origin_run_id,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        with self.database.session() as session:
            session.add(
                CommandCenterArtifactRow(
                    id=str(artifact_id),
                    current_revision=1,
                    sensitive=sensitive,
                    origin_run_id=str(origin_run_id) if origin_run_id else None,
                    expires_at=expires_at,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(self._revision_row(artifact))
        return artifact

    def find_by_origin_run(self, origin_run_id: UUID) -> WorkArtifact | None:
        with self.database.session() as session:
            row = session.scalar(
                select(CommandCenterArtifactRow).where(
                    CommandCenterArtifactRow.origin_run_id == str(origin_run_id)
                )
            )
            if row is None:
                return None
            revision = session.get(
                CommandCenterArtifactRevisionRow,
                (row.id, row.current_revision),
            )
            return None if revision is None else self._artifact(row, revision)

    def update(
        self,
        artifact_id: UUID,
        *,
        expected_revision: int,
        title: str,
        body: str,
    ) -> WorkArtifact:
        current = self.get(artifact_id)
        if current.revision != expected_revision:
            raise ValueError("Artifact revision changed; reload before saving")
        now = datetime.now(UTC)
        updated = WorkArtifact(
            artifact_id=current.artifact_id,
            revision=current.revision + 1,
            title=title,
            body=body,
            digest=artifact_digest(title, body, current.sources),
            sources=current.sources,
            sensitive=current.sensitive,
            origin_run_id=current.origin_run_id,
            created_at=current.created_at,
            updated_at=now,
            expires_at=current.expires_at,
        )
        with self.database.session() as session:
            row = session.get(CommandCenterArtifactRow, str(artifact_id))
            if row is None or row.current_revision != expected_revision:
                raise ValueError("Artifact revision changed; reload before saving")
            row.current_revision = updated.revision
            row.updated_at = now
            session.add(self._revision_row(updated))
            pending = tuple(
                session.scalars(
                    select(CommandCenterDeliveryRow).where(
                        CommandCenterDeliveryRow.artifact_id == str(artifact_id),
                        CommandCenterDeliveryRow.state.in_(
                            (
                                DeliveryState.PENDING.value,
                                DeliveryState.AWAITING_APPROVAL.value,
                            )
                        ),
                    )
                )
            )
            for delivery in pending:
                delivery.state = DeliveryState.INVALIDATED.value
                delivery.error = "Artifact revision changed after delivery review."
                delivery.updated_at = now
        return updated

    def get(self, artifact_id: UUID, revision: int | None = None) -> WorkArtifact:
        with self.database.session() as session:
            row = session.get(CommandCenterArtifactRow, str(artifact_id))
            if row is None:
                raise KeyError(f"Unknown work artifact: {artifact_id}")
            selected_revision = revision or row.current_revision
            revision_row = session.get(
                CommandCenterArtifactRevisionRow,
                (str(artifact_id), selected_revision),
            )
            if revision_row is None:
                raise KeyError(f"Unknown work artifact revision: {artifact_id}:{selected_revision}")
            return self._artifact(row, revision_row)

    def list(self, *, include_expired: bool = False) -> tuple[WorkArtifact, ...]:
        now = datetime.now(UTC)
        statement = select(CommandCenterArtifactRow).order_by(
            CommandCenterArtifactRow.updated_at.desc()
        )
        if not include_expired:
            statement = statement.where(CommandCenterArtifactRow.expires_at > now)
        with self.database.session() as session:
            rows = tuple(session.scalars(statement))
            values = []
            for row in rows:
                revision = session.get(
                    CommandCenterArtifactRevisionRow,
                    (row.id, row.current_revision),
                )
                if revision is not None:
                    values.append(self._artifact(row, revision))
            return tuple(values)

    def delete(self, artifact_id: UUID) -> WorkArtifact:
        artifact = self.get(artifact_id)
        with self.database.session() as session:
            session.execute(
                delete(CommandCenterDeliveryRow).where(
                    CommandCenterDeliveryRow.artifact_id == str(artifact_id)
                )
            )
            session.execute(
                delete(CommandCenterArtifactRevisionRow).where(
                    CommandCenterArtifactRevisionRow.artifact_id == str(artifact_id)
                )
            )
            row = session.get(CommandCenterArtifactRow, str(artifact_id))
            if row is not None:
                session.delete(row)
        return artifact

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        cutoff = _aware(now or datetime.now(UTC))
        with self.database.session() as session:
            ids = tuple(
                session.scalars(
                    select(CommandCenterArtifactRow.id).where(
                        CommandCenterArtifactRow.expires_at <= cutoff
                    )
                )
            )
            if not ids:
                return 0
            session.execute(
                delete(CommandCenterDeliveryRow).where(
                    CommandCenterDeliveryRow.artifact_id.in_(ids)
                )
            )
            session.execute(
                delete(CommandCenterArtifactRevisionRow).where(
                    CommandCenterArtifactRevisionRow.artifact_id.in_(ids)
                )
            )
            session.execute(
                delete(CommandCenterArtifactRow).where(CommandCenterArtifactRow.id.in_(ids))
            )
            return len(ids)

    def clear(self) -> tuple[int, int]:
        with self.database.session() as session:
            artifacts = len(tuple(session.scalars(select(CommandCenterArtifactRow.id))))
            deliveries = len(tuple(session.scalars(select(CommandCenterDeliveryRow.id))))
            session.execute(delete(CommandCenterDeliveryRow))
            session.execute(delete(CommandCenterArtifactRevisionRow))
            session.execute(delete(CommandCenterArtifactRow))
            return artifacts, deliveries

    def queue_delivery(
        self,
        artifact: WorkArtifact,
        destination: DestinationBinding,
        *,
        target_preview: str,
        ttl_minutes: int = 30,
    ) -> PendingDelivery:
        if not 1 <= ttl_minutes <= 1_440:
            raise ValueError("Delivery review lifetime must be between 1 and 1440 minutes")
        now = datetime.now(UTC)
        delivery = PendingDelivery(
            artifact_id=artifact.artifact_id,
            artifact_revision=artifact.revision,
            artifact_digest=artifact.digest,
            destination_binding_id=destination.binding_id,
            destination_digest=destination_digest(destination),
            target_preview=target_preview,
            expires_at=now + timedelta(minutes=ttl_minutes),
            created_at=now,
            updated_at=now,
        )
        with self.database.session() as session:
            session.add(
                CommandCenterDeliveryRow(
                    id=str(delivery.delivery_id),
                    artifact_id=str(delivery.artifact_id),
                    artifact_revision=delivery.artifact_revision,
                    artifact_digest=delivery.artifact_digest,
                    destination_binding_id=str(delivery.destination_binding_id),
                    destination_snapshot_json=json.dumps(
                        destination.model_dump(mode="json"),
                        sort_keys=True,
                    ),
                    destination_digest=delivery.destination_digest,
                    target_preview=target_preview,
                    state=delivery.state.value,
                    correlation_id=None,
                    result_status=None,
                    error=None,
                    expires_at=delivery.expires_at,
                    created_at=now,
                    updated_at=now,
                )
            )
        return delivery

    def delivery(self, delivery_id: UUID) -> PendingDelivery:
        with self.database.session() as session:
            row = session.get(CommandCenterDeliveryRow, str(delivery_id))
            if row is None:
                raise KeyError(f"Unknown pending delivery: {delivery_id}")
            return self._delivery(row)

    def delivery_snapshot(self, delivery_id: UUID) -> DestinationBinding:
        with self.database.session() as session:
            row = session.get(CommandCenterDeliveryRow, str(delivery_id))
            if row is None:
                raise KeyError(f"Unknown pending delivery: {delivery_id}")
            return DestinationBinding.model_validate(json.loads(row.destination_snapshot_json))

    def deliveries(self) -> tuple[PendingDelivery, ...]:
        with self.database.session() as session:
            return tuple(
                self._delivery(row)
                for row in session.scalars(
                    select(CommandCenterDeliveryRow).order_by(
                        CommandCenterDeliveryRow.created_at.desc()
                    )
                )
            )

    def delivery_for_artifact_destination(
        self,
        artifact_id: UUID,
        destination_binding_id: UUID,
    ) -> PendingDelivery | None:
        with self.database.session() as session:
            row = session.scalar(
                select(CommandCenterDeliveryRow)
                .where(
                    CommandCenterDeliveryRow.artifact_id == str(artifact_id),
                    CommandCenterDeliveryRow.destination_binding_id == str(destination_binding_id),
                )
                .order_by(CommandCenterDeliveryRow.created_at.desc())
            )
            return None if row is None else self._delivery(row)

    def update_delivery(
        self,
        delivery_id: UUID,
        state: DeliveryState,
        *,
        correlation_id: UUID | None = None,
        result_status: ResultStatus | None = None,
        error: str | None = None,
    ) -> PendingDelivery:
        with self.database.session() as session:
            row = session.get(CommandCenterDeliveryRow, str(delivery_id))
            if row is None:
                raise KeyError(f"Unknown pending delivery: {delivery_id}")
            row.state = state.value
            row.correlation_id = str(correlation_id) if correlation_id else row.correlation_id
            row.result_status = result_status.value if result_status else None
            row.error = error[:1_000] if error else None
            row.updated_at = datetime.now(UTC)
        return self.delivery(delivery_id)

    @staticmethod
    def _revision_row(artifact: WorkArtifact) -> CommandCenterArtifactRevisionRow:
        return CommandCenterArtifactRevisionRow(
            artifact_id=str(artifact.artifact_id),
            revision=artifact.revision,
            title=artifact.title,
            body=artifact.body,
            digest=artifact.digest,
            sources_json=json.dumps(
                [source.model_dump(mode="json") for source in artifact.sources],
                sort_keys=True,
            ),
            created_at=artifact.updated_at,
        )

    @staticmethod
    def _artifact(
        row: CommandCenterArtifactRow,
        revision: CommandCenterArtifactRevisionRow,
    ) -> WorkArtifact:
        return WorkArtifact(
            artifact_id=UUID(row.id),
            revision=revision.revision,
            title=revision.title,
            body=revision.body,
            digest=revision.digest,
            sources=tuple(
                ArtifactSourceReference.model_validate(value)
                for value in json.loads(revision.sources_json)
            ),
            sensitive=row.sensitive,
            origin_run_id=UUID(row.origin_run_id) if row.origin_run_id else None,
            created_at=_aware(row.created_at),
            updated_at=_aware(revision.created_at),
            expires_at=_aware(row.expires_at),
        )

    @staticmethod
    def _delivery(row: CommandCenterDeliveryRow) -> PendingDelivery:
        return PendingDelivery(
            delivery_id=UUID(row.id),
            artifact_id=UUID(row.artifact_id),
            artifact_revision=row.artifact_revision,
            artifact_digest=row.artifact_digest,
            destination_binding_id=UUID(row.destination_binding_id),
            destination_digest=row.destination_digest,
            target_preview=row.target_preview,
            state=DeliveryState(row.state),
            correlation_id=UUID(row.correlation_id) if row.correlation_id else None,
            result_status=ResultStatus(row.result_status) if row.result_status else None,
            error=row.error,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
            expires_at=_aware(row.expires_at),
        )
