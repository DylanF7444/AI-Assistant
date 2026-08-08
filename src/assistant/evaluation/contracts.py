from __future__ import annotations

import hashlib
import statistics
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationDisposition(StrEnum):
    SKILL = "skill"
    PLAN = "plan"
    ANSWER = "answer"
    CLARIFICATION = "clarification"
    UNSUPPORTED = "unsupported"
    PLANNER_REQUIRED = "planner_required"
    ERROR = "error"


class ScorerType(StrEnum):
    EXACT = "exact"
    RULES = "rules"
    SEMANTIC = "semantic"


class LatencyClass(StrEnum):
    IMMEDIATE = "immediate"
    INTERACTIVE = "interactive"
    MODEL = "model"
    BACKGROUND = "background"

    @property
    def maximum_ms(self) -> float:
        return {
            LatencyClass.IMMEDIATE: 100.0,
            LatencyClass.INTERACTIVE: 1_000.0,
            LatencyClass.MODEL: 65_000.0,
            LatencyClass.BACKGROUND: 120_000.0,
        }[self]


class PriorTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(pattern="^(user|assistant)$")
    text: str = Field(min_length=1, max_length=4_000)


class ExpectedOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: EvaluationDisposition
    allowed_dispositions: tuple[EvaluationDisposition, ...] = ()
    skill_ids: tuple[str, ...] = ()
    forbidden_skill_ids: tuple[str, ...] = ()
    arguments: dict[str, Any] = Field(default_factory=dict)
    clarification_terms: tuple[str, ...] = ()
    reference_answers: tuple[str, ...] = ()
    allowed_source_ids: tuple[str, ...] = ()


class EvaluationFamily(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,80}$")
    category: str = Field(min_length=2, max_length=100)
    requests: tuple[str, ...] = Field(min_length=1, max_length=25)
    prior_turns: tuple[PriorTurn, ...] = ()
    enabled_skills: tuple[str, ...] = ()
    permissions: dict[str, bool] = Field(default_factory=dict)
    expected: ExpectedOutcome
    expected_planner: ExpectedOutcome | None = None
    latency_class: LatencyClass
    safety_invariants: tuple[str, ...] = ()
    scorer_types: tuple[ScorerType, ...] = (ScorerType.EXACT, ScorerType.RULES)
    explanation: str = Field(min_length=3, max_length=1_000)


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    family_id: str
    category: str
    request: str
    prior_turns: tuple[PriorTurn, ...]
    enabled_skills: tuple[str, ...]
    permissions: dict[str, bool]
    expected: ExpectedOutcome
    expected_planner: ExpectedOutcome | None
    latency_class: LatencyClass
    safety_invariants: tuple[str, ...]
    scorer_types: tuple[ScorerType, ...]
    explanation: str


class EvaluationCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,80}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=3, max_length=1_000)
    minimum_success_rate: float = Field(default=0.95, ge=0, le=1)
    category_thresholds: dict[str, float] = Field(default_factory=dict)
    families: tuple[EvaluationFamily, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _family_ids_must_be_unique(self) -> EvaluationCorpus:
        ids = [family.family_id for family in self.families]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation family IDs must be unique")
        return self

    @classmethod
    def load(cls, path: Path) -> EvaluationCorpus:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def cases(self) -> tuple[EvaluationCase, ...]:
        expanded = []
        for family in self.families:
            for index, request in enumerate(family.requests, start=1):
                expanded.append(
                    EvaluationCase(
                        case_id=f"{family.family_id}-{index:03d}",
                        family_id=family.family_id,
                        category=family.category,
                        request=request,
                        prior_turns=family.prior_turns,
                        enabled_skills=family.enabled_skills,
                        permissions=family.permissions,
                        expected=family.expected,
                        expected_planner=family.expected_planner,
                        latency_class=family.latency_class,
                        safety_invariants=family.safety_invariants,
                        scorer_types=family.scorer_types,
                        explanation=family.explanation,
                    )
                )
        return tuple(expanded)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


class EvaluationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: EvaluationDisposition
    path: str = Field(default="router", pattern="^(router|planner)$")
    skill_ids: tuple[str, ...] = ()
    arguments: dict[str, Any] = Field(default_factory=dict)
    clarification: str | None = None
    answer: str | None = None
    source_ids: tuple[str, ...] = ()
    latency_ms: float = Field(ge=0)
    retry_count: int = Field(default=0, ge=0)
    unsupported_claims: tuple[str, ...] = ()
    error_code: str | None = None


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: str
    run: int = Field(ge=1)
    passed: bool
    score: float = Field(ge=0, le=1)
    reason_codes: tuple[str, ...]
    observation: EvaluationObservation


class CategorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    attempts: int
    passed: int
    success_rate: float
    confidence_low: float
    confidence_high: float


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_id: str
    corpus_version: str
    corpus_fingerprint: str
    created_at: datetime
    evaluator: str
    runs_per_case: int
    total_attempts: int
    passed_attempts: int
    success_rate: float
    categories: tuple[CategorySummary, ...]
    reason_counts: dict[str, int]
    median_latency_ms: float
    p95_latency_ms: float
    clarification_rate: float
    retry_rate: float
    unsupported_claim_count: int
    error_count: int
    thresholds_met: bool
    failed_thresholds: tuple[str, ...]
    results: tuple[EvaluationResult, ...]

    @classmethod
    def from_results(
        cls,
        corpus: EvaluationCorpus,
        results: tuple[EvaluationResult, ...],
        *,
        evaluator: str,
        runs_per_case: int,
    ) -> EvaluationReport:
        categories = []
        for category in sorted({result.category for result in results}):
            selected = [result for result in results if result.category == category]
            passed = sum(result.passed for result in selected)
            low, high = wilson_interval(passed, len(selected))
            categories.append(
                CategorySummary(
                    category=category,
                    attempts=len(selected),
                    passed=passed,
                    success_rate=passed / len(selected),
                    confidence_low=low,
                    confidence_high=high,
                )
            )
        passed_attempts = sum(result.passed for result in results)
        durations = sorted(result.observation.latency_ms for result in results)
        reasons = Counter(
            reason for result in results for reason in result.reason_codes if reason != "passed"
        )
        overall_rate = passed_attempts / len(results) if results else 0
        failed_thresholds = []
        if overall_rate < corpus.minimum_success_rate:
            failed_thresholds.append("overall")
        category_by_name = {item.category: item for item in categories}
        for category, threshold in corpus.category_thresholds.items():
            summary = category_by_name.get(category)
            if summary is not None and summary.success_rate < threshold:
                failed_thresholds.append(f"category:{category}")
        return cls(
            corpus_id=corpus.corpus_id,
            corpus_version=corpus.version,
            corpus_fingerprint=corpus.fingerprint,
            created_at=datetime.now(UTC),
            evaluator=evaluator,
            runs_per_case=runs_per_case,
            total_attempts=len(results),
            passed_attempts=passed_attempts,
            success_rate=overall_rate,
            categories=tuple(categories),
            reason_counts=dict(sorted(reasons.items())),
            median_latency_ms=statistics.median(durations) if durations else 0,
            p95_latency_ms=(durations[max(0, int(len(durations) * 0.95) - 1)] if durations else 0),
            clarification_rate=(
                sum(
                    result.observation.disposition == EvaluationDisposition.CLARIFICATION
                    for result in results
                )
                / len(results)
                if results
                else 0
            ),
            retry_rate=(
                sum(result.observation.retry_count > 0 for result in results) / len(results)
                if results
                else 0
            ),
            unsupported_claim_count=sum(
                len(result.observation.unsupported_claims) for result in results
            ),
            error_count=sum(bool(result.observation.error_code) for result in results),
            thresholds_met=not failed_thresholds,
            failed_thresholds=tuple(failed_thresholds),
            results=results,
        )

    def human_summary(self) -> str:
        lines = [
            f"Evaluation: {self.corpus_id} {self.corpus_version}",
            f"Evaluator: {self.evaluator}",
            (f"Success: {self.passed_attempts}/{self.total_attempts} ({self.success_rate:.1%})"),
            f"Latency: p50 {self.median_latency_ms:.1f} ms, p95 {self.p95_latency_ms:.1f} ms",
            (
                f"Clarifications: {self.clarification_rate:.1%}; retries: "
                f"{self.retry_rate:.1%}; errors: {self.error_count}"
            ),
            f"Regression thresholds: {'PASS' if self.thresholds_met else 'FAIL'}",
            "",
            "Category results:",
        ]
        lines.extend(
            (
                f"- {item.category}: {item.passed}/{item.attempts} "
                f"({item.success_rate:.1%}, 95% CI "
                f"{item.confidence_low:.1%}-{item.confidence_high:.1%})"
            )
            for item in self.categories
        )
        if self.reason_counts:
            lines.extend(("", "Failure reasons:"))
            lines.extend(f"- {reason}: {count}" for reason, count in self.reason_counts.items())
        return "\n".join(lines) + "\n"


def wilson_interval(successes: int, attempts: int) -> tuple[float, float]:
    if attempts == 0:
        return 0.0, 0.0
    z = 1.959963984540054
    proportion = successes / attempts
    denominator = 1 + z**2 / attempts
    center = (proportion + z**2 / (2 * attempts)) / denominator
    margin = (
        z
        * ((proportion * (1 - proportion) / attempts + z**2 / (4 * attempts**2)) ** 0.5)
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)
