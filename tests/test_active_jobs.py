import asyncio

from assistant.bootstrap import build_container
from assistant.contracts import PermissionContext, SkillCall


async def test_execution_engine_reports_and_cancels_active_timer(settings):
    container = build_container(settings)
    call = SkillCall(
        skill_id="core.timer",
        arguments={"seconds": 10, "label": "Tea"},
        permission_context=PermissionContext(user_id="test", session_id="test"),
        idempotency_key="active-timer",
    )

    task = asyncio.create_task(container.engine.execute(call))
    await asyncio.sleep(0)

    jobs = container.engine.active_jobs()
    assert len(jobs) == 1
    assert jobs[0].correlation_id == call.correlation_id
    assert jobs[0].skill_id == "core.timer"
    assert jobs[0].arguments["label"] == "Tea"

    await container.engine.cancel(call.correlation_id)
    result = await task

    assert result.status.value == "cancelled"
    assert container.engine.active_jobs() == ()
