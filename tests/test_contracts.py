from uuid import uuid4

import pytest
from pydantic import ValidationError

from assistant.contracts import AudioSession, Intent, Transcript


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
            confidence=2,
            source="rule",
            unexpected=True,
        )


def test_contracts_are_immutable():
    session = AudioSession(source="microphone")
    with pytest.raises(ValidationError):
        session.source = "file"

