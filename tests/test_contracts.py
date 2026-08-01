import pytest
from pydantic import ValidationError

from assistant.contracts import (
    AudioSession,
    Intent,
    RequestResultItem,
    ResultStatus,
    SkillResult,
    Transcript,
    WordTimestamp,
)


def test_contracts_are_strict_and_versioned():
    session = AudioSession(source="microphone")
    transcript = Transcript(
        session_id=session.session_id,
        raw_text="Hello",
        normalized_text="hello",
        confidence=0.9,
    )
    assert transcript.contract_version == "1.0"
    with pytest.raises(ValidationError):
        Intent(
            name="bad",
            confidence=101,
            source="rule",
            unexpected=True,
        )


def test_confidence_percentages_are_normalized_to_decimal_scale():
    session = AudioSession(source="microphone")
    transcript = Transcript(
        session_id=session.session_id,
        raw_text="Hello",
        normalized_text="hello",
        confidence=100,
        words=(
            WordTimestamp(
                word="Hello",
                start_seconds=0,
                end_seconds=0.2,
                confidence=75,
            ),
        ),
    )
    intent = Intent(name="core.help", confidence=95, source="rule")

    assert transcript.confidence == 1
    assert transcript.words[0].confidence == 0.75
    assert intent.confidence == 0.95


def test_contracts_are_immutable():
    session = AudioSession(source="microphone")
    with pytest.raises(ValidationError):
        session.source = "file"


def test_skill_result_rejects_duplicate_request_result_item_ids():
    item = RequestResultItem(item_id="result-1", title="One", value={"id": 1})

    with pytest.raises(ValidationError, match="item IDs must be unique"):
        SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Duplicate results.",
            items=(item, item),
        )
