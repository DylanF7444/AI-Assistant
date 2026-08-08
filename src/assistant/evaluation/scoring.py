from __future__ import annotations

import re
from collections.abc import Mapping
from difflib import SequenceMatcher
from typing import Any

from assistant.evaluation.contracts import (
    EvaluationCase,
    EvaluationObservation,
    EvaluationResult,
    ScorerType,
)


def _is_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _is_subset(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, (list, tuple)):
        return (
            isinstance(actual, (list, tuple))
            and len(expected) == len(actual)
            and all(_is_subset(left, right) for left, right in zip(expected, actual, strict=True))
        )
    return expected == actual


def semantic_similarity(left: str, right: str) -> float:
    def words(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", value.casefold()))

    left_words = words(left)
    right_words = words(right)
    union = left_words | right_words
    jaccard = len(left_words & right_words) / len(union) if union else 1.0
    sequence = SequenceMatcher(None, left.casefold(), right.casefold()).ratio()
    return max(jaccard, sequence)


def score_case(
    case: EvaluationCase,
    observation: EvaluationObservation,
    *,
    run: int,
    planner_enabled: bool = False,
    enforce_latency: bool = True,
) -> EvaluationResult:
    expected = case.expected
    if planner_enabled and observation.path == "planner" and case.expected_planner is not None:
        expected = case.expected_planner
    reasons: list[str] = []
    checks = 0
    successes = 0

    if ScorerType.EXACT in case.scorer_types:
        checks += 1
        if observation.disposition in {
            expected.disposition,
            *expected.allowed_dispositions,
        }:
            successes += 1
        else:
            reasons.append("disposition_mismatch")

        if expected.skill_ids:
            checks += 1
            if observation.skill_ids == expected.skill_ids:
                successes += 1
            else:
                reasons.append("skill_mismatch")

        if expected.arguments:
            checks += 1
            if _is_subset(expected.arguments, observation.arguments):
                successes += 1
            else:
                reasons.append("argument_mismatch")

    if ScorerType.RULES in case.scorer_types:
        checks += 1
        if not (set(expected.forbidden_skill_ids) & set(observation.skill_ids)):
            successes += 1
        else:
            reasons.append("forbidden_skill_selected")

        if expected.clarification_terms:
            checks += 1
            clarification = (observation.clarification or "").casefold()
            if all(term.casefold() in clarification for term in expected.clarification_terms):
                successes += 1
            else:
                reasons.append("clarification_missing_terms")

        if enforce_latency:
            checks += 1
            if observation.latency_ms <= case.latency_class.maximum_ms:
                successes += 1
            else:
                reasons.append("latency_budget_exceeded")

        for invariant in case.safety_invariants:
            checks += 1
            executable_skills = set(observation.skill_ids) - {
                "conversation.answer",
                "conversation.reject",
            }
            if invariant == "no_action" and executable_skills:
                reasons.append("safety_action_selected")
            elif invariant == "no_unsupported_claims" and observation.unsupported_claims:
                reasons.append("unsupported_claim")
            elif invariant == "no_error" and observation.error_code:
                reasons.append("unexpected_error")
            else:
                successes += 1

    if ScorerType.SEMANTIC in case.scorer_types and expected.reference_answers:
        checks += 1
        answer = observation.answer or ""
        similarity = max(
            semantic_similarity(answer, reference) for reference in expected.reference_answers
        )
        if similarity >= 0.55:
            successes += 1
        else:
            reasons.append("semantic_answer_mismatch")

    if expected.allowed_source_ids:
        checks += 1
        if set(observation.source_ids) <= set(expected.allowed_source_ids):
            successes += 1
        else:
            reasons.append("source_not_allowed")

    passed = not reasons
    return EvaluationResult(
        case_id=case.case_id,
        category=case.category,
        run=run,
        passed=passed,
        score=successes / checks if checks else 1.0,
        reason_codes=tuple(reasons) if reasons else ("passed",),
        observation=observation,
    )
