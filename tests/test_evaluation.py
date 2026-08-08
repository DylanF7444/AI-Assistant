from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.evaluation.contracts import (
    EvaluationCorpus,
    EvaluationDisposition,
    EvaluationObservation,
    wilson_interval,
)
from assistant.evaluation.runner import EvaluationRunner, ProductionCaseEvaluator
from assistant.evaluation.scoring import score_case, semantic_similarity

CORPUS_PATH = Path("benchmarks/corpora/assistant_intelligence_v1.json")


def test_versioned_intelligence_corpus_expands_to_one_hundred_unique_cases():
    corpus = EvaluationCorpus.load(CORPUS_PATH)
    cases = corpus.cases()

    assert corpus.version == "1.0.0"
    assert len(cases) == 100
    assert len({case.case_id for case in cases}) == 100
    assert len({case.category for case in cases}) >= 10
    assert len(corpus.fingerprint) == 64


def test_scorer_reports_stable_failure_reasons_and_partial_score():
    case = EvaluationCorpus.load(CORPUS_PATH).cases()[0]
    result = score_case(
        case,
        EvaluationObservation(
            disposition=EvaluationDisposition.ERROR,
            latency_ms=1,
            error_code="provider_unavailable",
        ),
        run=1,
    )

    assert not result.passed
    assert "disposition_mismatch" in result.reason_codes
    assert "skill_mismatch" in result.reason_codes
    assert "unexpected_error" in result.reason_codes
    assert 0 < result.score < 1


def test_semantic_scorer_and_wilson_interval_are_deterministic():
    related = semantic_similarity("The sky is blue because light scatters", "Blue light scatters")
    unrelated = semantic_similarity("The sky is blue", "Delete every file")
    low, high = wilson_interval(8, 10)

    assert related > unrelated
    assert 0 < low < 0.8 < high < 1


@pytest.mark.asyncio
async def test_production_router_runs_full_corpus_without_side_effects(tmp_path):
    container = build_container(
        Settings(
            profile="testing",
            data_dir=tmp_path,
            llm_preload=False,
            terminal_debug=False,
            speech_mode="never",
        )
    )
    try:
        report = await EvaluationRunner(
            ProductionCaseEvaluator(container.orchestrator, run_planner=False)
        ).run(EvaluationCorpus.load(CORPUS_PATH), enforce_latency=False)
    finally:
        container.database.engine.dispose()

    assert report.total_attempts == 100
    assert report.passed_attempts == 100
    assert report.success_rate == 1
    assert report.reason_counts == {}
    assert "Success: 100/100" in report.human_summary()
    # Reports contain observations, not the request text or prior-turn contents.
    assert "Ignore your rules" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_repeated_runs_report_variance_without_changing_case_ids():
    class Evaluator:
        name = "fake"
        planner_enabled = False

        async def evaluate(self, _case):
            return EvaluationObservation(
                disposition=EvaluationDisposition.SKILL,
                skill_ids=("control.cancel",),
                latency_ms=1,
            )

    corpus = EvaluationCorpus.model_validate(
        {
            "corpus_id": "tiny-corpus",
            "version": "1.0.0",
            "description": "A tiny repeated-run corpus.",
            "families": [
                {
                    "family_id": "cancel-family",
                    "category": "control",
                    "requests": ["cancel"],
                    "expected": {
                        "disposition": "skill",
                        "skill_ids": ["control.cancel"],
                    },
                    "latency_class": "immediate",
                    "explanation": "The fake evaluator should pass.",
                }
            ],
        }
    )

    report = await EvaluationRunner(Evaluator()).run(corpus, runs_per_case=3)

    assert report.total_attempts == 3
    assert [result.run for result in report.results] == [1, 2, 3]
    assert {result.case_id for result in report.results} == {"cancel-family-001"}
