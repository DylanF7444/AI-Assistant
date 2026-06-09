from __future__ import annotations

import tracemalloc

import pytest

from assistant.bootstrap import build_container
from assistant.contracts import PermissionContext, ResultStatus, SkillCall


@pytest.mark.asyncio
async def test_sustained_commands_keep_runtime_state_bounded(settings):
    container = build_container(settings)
    tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()

    for index in range(1500):
        result = await container.engine.execute(
            SkillCall(
                skill_id="core.calculator",
                arguments={"expression": f"{index} + 1"},
                permission_context=PermissionContext(session_id="stability"),
                idempotency_key=f"stability-{index}",
            )
        )
        assert result.status == ResultStatus.COMPLETED

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(container.engine._completed_keys) == 1024
    assert current - baseline < 16 * 1024 * 1024
    assert peak - baseline < 32 * 1024 * 1024
    assert container.database.integrity_check() is True
