from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from assistant.command_center import (
    DeliveryMode,
    DestinationBinding,
    SourceBinding,
    SourceUseMode,
)
from assistant.scheduling.contracts import (
    CommandCenterBriefAction,
    OneTimeTrigger,
    ScheduleRunState,
    ScheduleStatus,
)


def _due_now() -> OneTimeTrigger:
    return OneTimeTrigger(run_at=datetime.now(UTC))


def _container(settings):
    from assistant.bootstrap import build_container

    return build_container(
        settings.model_copy(
            update={
                "background_actions_enabled": True,
                "scheduler_enabled": True,
            }
        )
    )


def _tasks_source(container) -> SourceBinding:
    return container.command_center.save_source(
        SourceBinding(
            source_id="local.tasks",
            display_name="My tasks",
            configuration={},
            use_modes=(SourceUseMode.BRIEFING,),
        )
    )


def test_scheduled_brief_freezes_sources_and_publishes_a_durable_report(settings):
    container = _container(settings)
    source = _tasks_source(container)

    schedule = container.command_center_authoring.create_command_center_brief(
        command_center=container.command_center,
        name="Morning brief",
        trigger=_due_now(),
        binding_ids=(source.binding_id,),
        title="Morning Work Brief",
        retention_days=7,
    )

    assert isinstance(schedule.action, CommandCenterBriefAction)
    assert schedule.action.sources[0].call.caller == "scheduler"
    assert schedule.action.sources[0].call.retain_audit_content is False
    assert schedule.autonomy_policy.allowed_skill_ids == ("core.tasks",)
    assert schedule.status == ScheduleStatus.ACTIVE

    assert asyncio.run(container.scheduler.run_once()) == 1

    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    artifact = container.command_center.work_artifacts()[0]
    assert run.state == ScheduleRunState.SUCCEEDED
    assert run.result_status == "completed"
    assert run.result == {
        "artifact_id": str(artifact.artifact_id),
        "artifact_revision": 1,
        "delivery_id": None,
        "sources": {
            f"brief-{source.binding_id.hex}": {
                "status": "completed",
                "error_code": None,
                "content_omitted": True,
            }
        },
        "content_omitted": True,
    }
    assert artifact.origin_run_id == run.run_id
    assert artifact.title == "Morning Work Brief"
    assert "My tasks" in artifact.body
    report = container.notification_center.history()[0]
    assert report.notification_id == run.run_id
    assert report.schedule_id == schedule.schedule_id
    assert report.run_id == run.run_id


def test_scheduled_brief_binding_change_revokes_authority_before_collection(settings):
    container = _container(settings)
    source = _tasks_source(container)
    schedule = container.command_center_authoring.create_command_center_brief(
        command_center=container.command_center,
        name="Frozen brief",
        trigger=_due_now(),
        binding_ids=(source.binding_id,),
    )
    container.command_center.save_source(source.model_copy(update={"max_items": 50}))

    assert asyncio.run(container.scheduler.run_once()) == 1

    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    revised = container.schedule_store.get(schedule.schedule_id)
    assert run.state == ScheduleRunState.BLOCKED
    assert "input changed" in (run.blocked_reason or "").casefold()
    assert revised.status == ScheduleStatus.PENDING_REVIEW
    assert container.command_center.work_artifacts() == ()


def test_scheduled_brief_queues_configured_output_without_delivering_it(settings):
    container = _container(settings)
    source = _tasks_source(container)
    destination = container.command_center.save_destination(
        DestinationBinding(
            destination_id="local.inbox",
            display_name="Reviewed Inbox copy",
            configuration={},
            delivery_mode=DeliveryMode.LOCAL,
            allow_sensitive=True,
        )
    )
    schedule = container.command_center_authoring.create_command_center_brief(
        command_center=container.command_center,
        name="Brief with reviewed output",
        trigger=_due_now(),
        binding_ids=(source.binding_id,),
        destination_binding_id=destination.binding_id,
    )

    assert asyncio.run(container.scheduler.run_once()) == 1

    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    pending = container.command_center.pending_deliveries()[0]
    assert run.state == ScheduleRunState.SUCCEEDED
    assert run.result is not None
    assert run.result["delivery_id"] == str(pending.delivery_id)
    assert pending.target_preview == "Local Assistant Inbox"
    assert pending.state.value == "pending"
    assert (
        sum(message.category == "report" for message in container.notification_center.history())
        == 1
    )


def test_scheduled_artifact_save_is_idempotent_for_one_run(settings):
    container = _container(settings)
    source = _tasks_source(container)
    schedule = container.command_center_authoring.create_command_center_brief(
        command_center=container.command_center,
        name="Idempotent brief",
        trigger=_due_now(),
        binding_ids=(source.binding_id,),
    )
    asyncio.run(container.scheduler.run_once())
    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    original = container.command_center.work_artifacts()[0]
    brief = container.command_center.build_brief((), title="Should not replace saved work")

    duplicate = container.command_center.save_brief(brief, origin_run_id=run.run_id)

    assert duplicate == original
    assert container.command_center.work_artifacts() == (original,)
