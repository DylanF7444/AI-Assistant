from __future__ import annotations

import hashlib
import json
import re
import wave
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SpeechCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]+$", max_length=100)
    audio: str = Field(min_length=1, max_length=500)
    reference: str = Field(default="", max_length=2_000)
    condition: Literal[
        "clean",
        "quiet-speech",
        "controlled-noise",
        "accent",
        "long-form",
        "silence",
        "echo",
    ]
    expected_behavior: Literal["transcription", "silence"] = "transcription"
    speaker_id: str = Field(min_length=1, max_length=100)
    language: str = Field(default="en", min_length=2, max_length=20)
    notes: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def reference_matches_behavior(self) -> SpeechCase:
        if self.expected_behavior == "transcription" and not self.reference.strip():
            raise ValueError("transcription cases require a reference")
        if self.expected_behavior == "silence" and self.reference.strip():
            raise ValueError("silence cases must use an empty reference")
        path = Path(self.audio)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("audio must be a relative path inside the corpus asset root")
        return self


class SpeechCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]+$", max_length=100)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: Literal["template", "ready"]
    language: str = Field(default="en", min_length=2, max_length=20)
    license: str = Field(min_length=1, max_length=300)
    consent_record: str = Field(min_length=1, max_length=500)
    sample_format: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate_hz: int = Field(default=16_000, ge=8_000, le=48_000)
    channels: Literal[1] = 1
    thresholds: dict[str, float] = Field(default_factory=dict)
    cases: tuple[SpeechCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def cases_are_unique_and_match_language(self) -> SpeechCorpus:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("speech case IDs must be unique")
        if any(case.language != self.language for case in self.cases):
            raise ValueError("speech case language must match the corpus language")
        for name, value in self.thresholds.items():
            if name not in {"quiet-speech", "controlled-noise"}:
                raise ValueError(f"unsupported speech threshold: {name}")
            if not 0 <= value <= 1:
                raise ValueError("speech WER thresholds must be between zero and one")
        return self


class ResolvedSpeechCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case: SpeechCase
    path: Path
    duration_seconds: float = Field(gt=0)


class ResolvedSpeechCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_path: Path
    asset_root: Path
    corpus: SpeechCorpus
    cases: tuple[ResolvedSpeechCase, ...]
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class SpeechCorpusUnavailable(ValueError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(problems))


def load_speech_corpus(
    manifest_path: Path,
    *,
    asset_root: Path | None = None,
    require_ready: bool = True,
) -> ResolvedSpeechCorpus:
    manifest_path = manifest_path.resolve()
    corpus = SpeechCorpus.model_validate_json(manifest_path.read_text("utf-8"))
    root = (asset_root or manifest_path.parent).resolve()
    problems = []
    if require_ready and corpus.status != "ready":
        problems.append(
            "corpus status is template; record or supply the consented audio assets and set status "
            "to ready"
        )
    resolved = []
    hashes = []
    for case in corpus.cases:
        path = (root / case.audio).resolve()
        if root != path and root not in path.parents:
            problems.append(f"{case.case_id}: audio escapes the asset root")
            continue
        if not path.is_file():
            problems.append(f"{case.case_id}: missing audio asset {case.audio}")
            continue
        try:
            with wave.open(str(path), "rb") as audio:
                if audio.getsampwidth() != 2:
                    problems.append(f"{case.case_id}: WAV must use signed 16-bit PCM")
                    continue
                if audio.getnchannels() != corpus.channels:
                    problems.append(f"{case.case_id}: WAV must be mono")
                    continue
                if audio.getframerate() != corpus.sample_rate_hz:
                    problems.append(
                        f"{case.case_id}: WAV sample rate must be {corpus.sample_rate_hz} Hz"
                    )
                    continue
                duration = audio.getnframes() / audio.getframerate()
                if duration <= 0:
                    problems.append(f"{case.case_id}: WAV is empty")
                    continue
        except (OSError, EOFError, wave.Error) as exc:
            problems.append(f"{case.case_id}: invalid WAV ({exc.__class__.__name__})")
            continue
        payload_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.append((case.case_id, payload_hash))
        resolved.append(ResolvedSpeechCase(case=case, path=path, duration_seconds=duration))
    if problems:
        raise SpeechCorpusUnavailable(problems)
    digest_payload = json.dumps(
        {
            "manifest": corpus.model_dump(mode="json"),
            "assets": hashes,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return ResolvedSpeechCorpus(
        manifest_path=manifest_path,
        asset_root=root,
        corpus=corpus,
        cases=tuple(resolved),
        digest=hashlib.sha256(digest_payload.encode("utf-8")).hexdigest(),
    )


def normalized_words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", value.casefold())


def word_error_rate(reference: str, hypothesis: str) -> float:
    reference_words = normalized_words(reference)
    hypothesis_words = normalized_words(hypothesis)
    distances = list(range(len(hypothesis_words) + 1))
    for index, reference_word in enumerate(reference_words, 1):
        next_distances = [index]
        for offset, hypothesis_word in enumerate(hypothesis_words, 1):
            next_distances.append(
                min(
                    next_distances[-1] + 1,
                    distances[offset] + 1,
                    distances[offset - 1] + (reference_word != hypothesis_word),
                )
            )
        distances = next_distances
    return distances[-1] / max(1, len(reference_words))


def percentile(values: list[float], percentile_value: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile_value
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def speech_condition_summary(samples: list[dict], thresholds: dict[str, float]) -> dict:
    summaries = {}
    conditions = sorted({str(sample["condition"]) for sample in samples})
    for condition in conditions:
        group = [sample for sample in samples if sample["condition"] == condition]
        word_errors = [float(sample["word_error_rate"]) for sample in group]
        partials = [
            float(sample["first_partial_seconds"])
            for sample in group
            if sample.get("first_partial_seconds") is not None
        ]
        finals = [float(sample["final_seconds"]) for sample in group]
        mean_wer = sum(word_errors) / len(word_errors)
        threshold = thresholds.get(condition)
        summaries[condition] = {
            "count": len(group),
            "mean_word_error_rate": mean_wer,
            "word_error_rate_threshold": threshold,
            "word_error_rate_passed": threshold is None or mean_wer <= threshold,
            "first_partial_p50_seconds": percentile(partials, 0.50),
            "first_partial_p95_seconds": percentile(partials, 0.95),
            "final_p50_seconds": percentile(finals, 0.50),
            "final_p95_seconds": percentile(finals, 0.95),
        }
    return summaries
