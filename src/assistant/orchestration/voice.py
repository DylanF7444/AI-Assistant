from __future__ import annotations

from dataclasses import dataclass

from assistant.contracts import Intent, Transcript, WordTimestamp

SENSITIVE_ARGUMENT_KEYS = frozenset(
    {
        "path",
        "source",
        "destination",
        "recipient",
        "amount",
        "device_id",
        "pid",
        "process_name",
        "entity_id",
    }
)
SENSITIVE_INTENTS = frozenset(
    {
        "windows.file_operation",
        "windows.process",
        "windows.input_automation",
        "integration.device_control",
        "integration.home_assistant",
    }
)


@dataclass(frozen=True)
class VoiceGateDecision:
    should_clarify: bool
    uncertain: bool
    sensitive: bool
    low_confidence_words: tuple[WordTimestamp, ...]
    sensitive_argument_keys: tuple[str, ...]


def evaluate_voice_gate(
    transcript: Transcript,
    intent: Intent | None,
) -> VoiceGateDecision:
    low_words = tuple(
        word for word in transcript.words if word.confidence is not None and word.confidence < 0.65
    )
    uncertain = (transcript.confidence is not None and transcript.confidence < 0.75) or bool(
        low_words
    )
    argument_keys = tuple(
        sorted(SENSITIVE_ARGUMENT_KEYS.intersection(intent.arguments)) if intent else ()
    )
    sensitive = bool(intent and (argument_keys or intent.name in SENSITIVE_INTENTS))
    return VoiceGateDecision(
        should_clarify=uncertain and sensitive,
        uncertain=uncertain,
        sensitive=sensitive,
        low_confidence_words=low_words,
        sensitive_argument_keys=argument_keys,
    )
