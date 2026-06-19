from uuid import uuid4

import pytest
from pydantic import ValidationError

from assistant.contracts import AudioSession, Intent, Transcript


def test_contract_defaults_make_related_objects_easy_to_connect():
    """A session ID links audio capture to its transcript."""
    session = AudioSession(source="microphone")

    transcript = Transcript(
        session_id=session.session_id,
        raw_text="Hello Assistant",
        normalized_text="hello assistant",
    )

    assert transcript.session_id == session.session_id
    assert transcript.contract_version == "1.0"
    assert transcript.is_final is True


def test_contract_validation_rejects_impossible_confidence():
    """Pydantic enforces the confidence boundary after percent normalization."""
    with pytest.raises(ValidationError):
        Intent(
            name="core.help",
            confidence=101,
            source="rule",
        )


def test_contracts_are_immutable_after_creation():
    transcript = Transcript(
        session_id=uuid4(),
        raw_text="Original",
        normalized_text="original",
    )

    with pytest.raises(ValidationError):
        transcript.raw_text = "Changed"
