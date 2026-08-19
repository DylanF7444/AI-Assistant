from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assistant.bootstrap import build_container
from assistant.command_center import (
    DeliveryMode,
    DeliveryState,
    DestinationBinding,
    SourceResult,
    WorkBrief,
)
from assistant.config import Settings
from assistant.contracts import PermissionContext, ResultStatus
from assistant.policy.permissions import GrantType


def _brief(*, sensitive: bool = False, body: str = "Reviewed work body.") -> WorkBrief:
    source = SourceResult(
        binding_id="00000000-0000-0000-0000-000000000001",
        source_id="local.tasks",
        provider_id="local",
        display_name="Tasks",
        status=ResultStatus.COMPLETED,
        summary="One task.",
        sensitive=sensitive,
    )
    return WorkBrief(
        status=ResultStatus.COMPLETED,
        content=body,
        sources=(source,),
        sensitive=sensitive,
    )


def test_artifact_save_is_explicit_revisioned_bounded_and_content_safe_in_audit(tmp_path):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    assert container.command_center.work_artifacts() == ()

    artifact = container.command_center.save_brief(
        _brief(body="private reviewed plan"),
        retention_days=7,
    )
    revised = container.command_center.edit_artifact(
        artifact.artifact_id,
        expected_revision=1,
        title="Edited brief",
        body="edited private plan",
    )

    assert artifact.revision == 1
    assert revised.revision == 2
    assert revised.digest != artifact.digest
    assert container.command_center.work_artifact(artifact.artifact_id, 1) == artifact
    assert container.command_center.work_artifact(artifact.artifact_id) == revised
    assert revised.expires_at <= datetime.now(UTC) + timedelta(days=8)
    audit = str(container.audit.query())
    assert "private reviewed plan" not in audit
    assert "edited private plan" not in audit
    assert revised.digest in audit

    with pytest.raises(ValueError, match="revision changed"):
        container.command_center.edit_artifact(
            artifact.artifact_id,
            expected_revision=1,
            title="Stale",
            body="stale edit",
        )


def test_artifact_edit_invalidates_pending_delivery_and_clear_removes_both(tmp_path):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    artifact = container.command_center.save_brief(_brief())
    destination = container.command_center.save_destination(
        DestinationBinding(
            destination_id="local.inbox",
            display_name="Inbox",
            delivery_mode=DeliveryMode.LOCAL,
            allow_sensitive=True,
        )
    )
    pending = container.command_center.queue_delivery(
        artifact.artifact_id,
        destination.binding_id,
    )

    container.command_center.edit_artifact(
        artifact.artifact_id,
        expected_revision=artifact.revision,
        title="Changed",
        body="Changed after review.",
    )

    invalidated = container.command_center.pending_deliveries()[0]
    assert invalidated.delivery_id == pending.delivery_id
    assert invalidated.state == DeliveryState.INVALIDATED
    assert container.command_center.clear_artifact_data() == (1, 1)
    assert container.command_center.work_artifacts() == ()
    assert container.command_center.pending_deliveries() == ()


@pytest.mark.asyncio
async def test_reviewed_local_inbox_delivery_revalidates_and_records_lifecycle(tmp_path):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    artifact = container.command_center.save_brief(_brief(sensitive=True))
    destination = container.command_center.save_destination(
        DestinationBinding(
            destination_id="local.inbox",
            display_name="Inbox",
            delivery_mode=DeliveryMode.LOCAL,
            allow_sensitive=True,
        )
    )
    pending = container.command_center.queue_delivery(
        artifact.artifact_id,
        destination.binding_id,
    )

    updated, outcome = await container.command_center.prepare_delivery(
        pending.delivery_id,
        PermissionContext(session_id="delivery"),
    )

    assert outcome.result is not None
    assert outcome.result.status == ResultStatus.COMPLETED
    assert updated.state == DeliveryState.SUCCEEDED
    assert updated.target_preview == "Local Assistant Inbox"
    assert container.notification_center.notifications()[0].category == "report"
    audit = container.audit.query(event_type="command_center_delivery_finished")
    assert audit[0]["details"]["content_omitted"] is True


@pytest.mark.asyncio
async def test_reviewed_export_requires_permission_confirmation_and_never_overwrites(tmp_path):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    export_path = tmp_path / "exports" / "brief.md"
    artifact = container.command_center.save_brief(_brief(body="exported body"))
    destination = container.command_center.save_destination(
        DestinationBinding(
            destination_id="local.export",
            display_name="Markdown export",
            configuration={"path": str(export_path), "format": "markdown"},
            delivery_mode=DeliveryMode.LOCAL,
            allow_sensitive=True,
        )
    )
    pending = container.command_center.queue_delivery(
        artifact.artifact_id,
        destination.binding_id,
    )

    awaiting_permission, permission_outcome = await container.command_center.prepare_delivery(
        pending.delivery_id,
        PermissionContext(session_id="delivery"),
    )
    assert awaiting_permission.state == DeliveryState.AWAITING_APPROVAL, (
        permission_outcome.result,
        permission_outcome.permission_requests,
    )
    assert permission_outcome.permission_requests
    for request in permission_outcome.permission_requests:
        container.permissions.grant(
            user_id="local-user",
            skill_id=request.call.skill_id,
            capability=request.scope.capability,
            grant_type=GrantType.SESSION,
            targets=request.scope.targets,
            session_id="delivery",
        )

    awaiting_confirmation, confirmation_outcome = await container.command_center.prepare_delivery(
        pending.delivery_id,
        PermissionContext(session_id="delivery"),
    )
    assert awaiting_confirmation.state == DeliveryState.AWAITING_APPROVAL
    assert confirmation_outcome.confirmation is not None

    completed, result = await container.command_center.confirm_delivery(
        pending.delivery_id,
        confirmation_outcome.confirmation.confirmation_id,
        PermissionContext(session_id="delivery"),
    )

    assert result.status == ResultStatus.COMPLETED
    assert completed.state == DeliveryState.SUCCEEDED
    assert export_path.read_text(encoding="utf-8") == "exported body"

    second_artifact = container.command_center.save_brief(_brief(body="must not overwrite"))
    second = container.command_center.queue_delivery(
        second_artifact.artifact_id,
        destination.binding_id,
    )
    _, second_confirmation = await container.command_center.prepare_delivery(
        second.delivery_id,
        PermissionContext(session_id="delivery"),
    )
    assert second_confirmation.confirmation is not None
    failed, failed_result = await container.command_center.confirm_delivery(
        second.delivery_id,
        second_confirmation.confirmation.confirmation_id,
        PermissionContext(session_id="delivery"),
    )
    assert failed_result.status == ResultStatus.FAILED
    assert failed.state == DeliveryState.FAILED
    assert export_path.read_text(encoding="utf-8") == "exported body"


@pytest.mark.asyncio
async def test_sensitive_artifact_requires_destination_opt_in_and_binding_drift_blocks_send(
    tmp_path,
):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    artifact = container.command_center.save_brief(_brief(sensitive=True))
    destination = container.command_center.save_destination(
        DestinationBinding(
            destination_id="local.inbox",
            display_name="Inbox",
            delivery_mode=DeliveryMode.LOCAL,
            allow_sensitive=False,
        )
    )
    with pytest.raises(PermissionError, match="sensitive"):
        container.command_center.queue_delivery(
            artifact.artifact_id,
            destination.binding_id,
        )

    approved = container.command_center.save_destination(
        destination.model_copy(update={"allow_sensitive": True})
    )
    pending = container.command_center.queue_delivery(
        artifact.artifact_id,
        approved.binding_id,
    )
    container.command_center.save_destination(
        approved.model_copy(update={"display_name": "Changed Inbox"})
    )

    with pytest.raises(ValueError, match="Destination changed"):
        # Revalidation happens before any local or external side effect.
        await container.command_center.prepare_delivery(
            pending.delivery_id,
            PermissionContext(session_id="delivery"),
        )
    assert container.command_center.pending_deliveries()[0].state == DeliveryState.INVALIDATED
