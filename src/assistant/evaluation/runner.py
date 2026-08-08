from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from assistant.evaluation.contracts import (
    EvaluationCase,
    EvaluationCorpus,
    EvaluationDisposition,
    EvaluationObservation,
    EvaluationReport,
)
from assistant.evaluation.scoring import score_case
from assistant.language.planner import PlannerDecision
from assistant.orchestrator import AssistantOrchestrator


class CaseEvaluator(Protocol):
    name: str
    planner_enabled: bool

    async def evaluate(self, case: EvaluationCase) -> EvaluationObservation: ...


class ProductionCaseEvaluator:
    """Evaluate through the production router and, optionally, planner pipeline.

    Planner evaluation calls the same retrieval, context, candidate selection, prompt,
    schema validation, and repair path used by the orchestrator, while deliberately
    stopping before policy preparation or skill execution.
    """

    def __init__(
        self,
        orchestrator: AssistantOrchestrator,
        *,
        run_planner: bool,
    ) -> None:
        self.orchestrator = orchestrator
        self.run_planner = run_planner
        self.planner_enabled = run_planner
        self.name = "production-planner" if run_planner else "production-router"

    async def evaluate(self, case: EvaluationCase) -> EvaluationObservation:
        started = time.perf_counter()
        active_path = "router"
        self.orchestrator.state.clear()
        self._seed_prior_turns(case)
        try:
            route = self.orchestrator.router.route(case.request)
            if route.clarification:
                return self._observation(
                    started,
                    EvaluationDisposition.CLARIFICATION,
                    clarification=route.clarification,
                )
            if not route.needs_llm:
                intents = route.intents or ((route.intent,) if route.intent is not None else ())
                skill_ids = tuple(intent.name for intent in intents)
                arguments = {
                    intent.name: intent.arguments for intent in intents if intent is not None
                }
                answer = next(
                    (
                        str(intent.arguments.get("text", ""))
                        for intent in intents
                        if intent is not None and intent.name == "conversation.answer"
                    ),
                    None,
                )
                disposition = (
                    EvaluationDisposition.ANSWER
                    if skill_ids == ("conversation.answer",)
                    else EvaluationDisposition.SKILL
                )
                return self._observation(
                    started,
                    disposition,
                    skill_ids=skill_ids,
                    arguments=arguments,
                    answer=answer,
                )
            if not self.run_planner:
                return self._observation(started, EvaluationDisposition.PLANNER_REQUIRED)
            if self.orchestrator.planner is None:
                return self._observation(
                    started,
                    EvaluationDisposition.ERROR,
                    path="planner",
                    error_code="planner_unavailable",
                )
            active_path = "planner"
            decision = await self.orchestrator._interpret_planner_goal(
                uuid4(),
                case.request,
                session_id=f"evaluation:{case.case_id}",
            )
            return self._planner_observation(started, decision)
        except TimeoutError:
            return self._observation(
                started,
                EvaluationDisposition.ERROR,
                path=active_path,
                error_code="timeout",
            )
        except Exception as error:  # evaluator must preserve the rest of the corpus
            return self._observation(
                started,
                EvaluationDisposition.ERROR,
                path=active_path,
                error_code=type(error).__name__,
            )

    def _seed_prior_turns(self, case: EvaluationCase) -> None:
        pending_request = ""
        for turn in case.prior_turns:
            if turn.role == "user":
                pending_request = turn.text
                continue
            self.orchestrator.state.session_context.append(
                {
                    "request": pending_request,
                    "intent": "evaluation.prior_turn",
                    "outcome": "completed",
                    "response_type": "answer",
                    "severity": "info",
                    "response": turn.text[:1_000],
                }
            )
            pending_request = ""

    def _planner_observation(
        self,
        started: float,
        decision: PlannerDecision | None,
    ) -> EvaluationObservation:
        if decision is None:
            return self._observation(
                started,
                EvaluationDisposition.ERROR,
                path="planner",
                error_code="empty_planner_decision",
            )
        if decision.clarification:
            return self._observation(
                started,
                EvaluationDisposition.CLARIFICATION,
                path="planner",
                clarification=decision.clarification,
            )
        intents = (
            tuple(step.intent for step in decision.steps)
            if decision.steps
            else ((decision.intent,) if decision.intent is not None else ())
        )
        if not intents:
            return self._observation(
                started,
                EvaluationDisposition.UNSUPPORTED,
                path="planner",
            )
        skill_ids = tuple(intent.name for intent in intents)
        arguments = {intent.name: intent.arguments for intent in intents}
        answer = next(
            (
                str(intent.arguments.get("text", ""))
                for intent in intents
                if intent.name == "conversation.answer"
            ),
            None,
        )
        disposition = (
            EvaluationDisposition.UNSUPPORTED
            if skill_ids == ("conversation.reject",)
            else EvaluationDisposition.ANSWER
            if skill_ids == ("conversation.answer",)
            else EvaluationDisposition.PLAN
            if decision.steps
            else EvaluationDisposition.SKILL
        )
        source_ids = tuple(
            str(getattr(item, "source_id", ""))
            for item in decision.retrieved
            if getattr(item, "source_id", None)
        )
        return self._observation(
            started,
            disposition,
            path="planner",
            skill_ids=skill_ids,
            arguments=arguments,
            answer=answer,
            source_ids=source_ids,
        )

    @staticmethod
    def _observation(
        started: float,
        disposition: EvaluationDisposition,
        **values: object,
    ) -> EvaluationObservation:
        return EvaluationObservation(
            disposition=disposition,
            latency_ms=(time.perf_counter() - started) * 1000,
            **values,
        )


class EvaluationRunner:
    def __init__(self, evaluator: CaseEvaluator) -> None:
        self.evaluator = evaluator

    async def run(
        self,
        corpus: EvaluationCorpus,
        *,
        runs_per_case: int = 1,
        categories: frozenset[str] | None = None,
        progress: Callable[[int, int], Awaitable[None] | None] | None = None,
        enforce_latency: bool = True,
    ) -> EvaluationReport:
        if runs_per_case < 1:
            raise ValueError("runs_per_case must be at least one")
        cases = tuple(
            case for case in corpus.cases() if categories is None or case.category in categories
        )
        if not cases:
            raise ValueError("no evaluation cases matched the selected categories")
        total = len(cases) * runs_per_case
        results = []
        completed = 0
        for run in range(1, runs_per_case + 1):
            for case in cases:
                observation = await self.evaluator.evaluate(case)
                results.append(
                    score_case(
                        case,
                        observation,
                        run=run,
                        planner_enabled=self.evaluator.planner_enabled,
                        enforce_latency=enforce_latency,
                    )
                )
                completed += 1
                if progress is not None:
                    update = progress(completed, total)
                    if update is not None:
                        await update
        return EvaluationReport.from_results(
            corpus,
            tuple(results),
            evaluator=self.evaluator.name,
            runs_per_case=runs_per_case,
        )


def write_report(report: EvaluationReport, output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report.corpus_id}-{report.corpus_version}-{report.evaluator}"
    json_path = output_dir / f"{stem}.json"
    text_path = output_dir / f"{stem}.txt"
    failures_path = output_dir / f"{stem}-failures.json"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    text_path.write_text(report.human_summary(), encoding="utf-8")
    failures_path.write_text(
        json.dumps(
            [
                {
                    "case_id": result.case_id,
                    "category": result.category,
                    "run": result.run,
                    "reason_codes": result.reason_codes,
                    "disposition": result.observation.disposition,
                    "path": result.observation.path,
                    "skill_ids": result.observation.skill_ids,
                    "latency_ms": result.observation.latency_ms,
                    "error_code": result.observation.error_code,
                }
                for result in report.results
                if not result.passed
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return json_path, text_path, failures_path
