"""Privacy-safe evaluation contracts and production-path runners."""

from assistant.evaluation.contracts import (
    EvaluationCase,
    EvaluationCorpus,
    EvaluationObservation,
    EvaluationReport,
)
from assistant.evaluation.runner import EvaluationRunner, ProductionCaseEvaluator
from assistant.evaluation.speech import (
    SpeechCorpus,
    SpeechCorpusUnavailable,
    load_speech_corpus,
    word_error_rate,
)

__all__ = [
    "EvaluationCase",
    "EvaluationCorpus",
    "EvaluationObservation",
    "EvaluationReport",
    "EvaluationRunner",
    "ProductionCaseEvaluator",
    "SpeechCorpus",
    "SpeechCorpusUnavailable",
    "load_speech_corpus",
    "word_error_rate",
]
