from pathlib import Path

import pytest

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.contracts import ResponseType, ResultStatus


@pytest.mark.asyncio
async def test_text_command_travels_through_the_complete_application(
    tmp_path: Path,
):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        retain_transcripts_days=0,
    )
    container = build_container(settings)

    response, outcome = await container.orchestrator.handle_text(
        "calculate 9 plus 3"
    )

    assert outcome.result.status == ResultStatus.COMPLETED
    assert outcome.result.data["value"] == 12
    assert response.response_type == ResponseType.SKILL
    assert "12" in response.display_text
