from __future__ import annotations

import argparse
import asyncio
import logging
import tempfile
from pathlib import Path

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.evaluation.contracts import EvaluationCorpus
from assistant.evaluation.runner import (
    EvaluationRunner,
    ProductionCaseEvaluator,
    write_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the privacy-safe assistant quality corpus.")
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("benchmarks/corpora/assistant_intelligence_v1.json"),
    )
    parser.add_argument("--output", type=Path, default=Path(".benchmark-data/evaluations"))
    parser.add_argument("--planner", action="store_true", help="Run the configured Ollama planner")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Run only this category; may be supplied more than once",
    )
    parser.add_argument(
        "--keep-data",
        type=Path,
        help="Use this data directory instead of an isolated temporary directory",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    corpus = EvaluationCorpus.load(args.corpus)
    temporary = None
    if args.keep_data is None:
        temporary = tempfile.TemporaryDirectory(prefix="assistant-evaluation-")
        data_dir = Path(temporary.name)
    else:
        data_dir = args.keep_data
    try:
        settings = Settings(
            profile="testing",
            data_dir=data_dir,
            llm_preload=False,
            terminal_debug=False,
            speech_mode="never",
        )
        container = build_container(settings)
        evaluator = ProductionCaseEvaluator(
            container.orchestrator,
            run_planner=args.planner,
        )

        def progress(completed: int, total: int) -> None:
            if completed == total or completed % 5 == 0:
                print(f"Progress: {completed}/{total}", flush=True)

        report = await EvaluationRunner(evaluator).run(
            corpus,
            runs_per_case=args.runs,
            categories=frozenset(args.category) if args.category else None,
            progress=progress,
        )
        json_path, text_path, failures_path = write_report(report, args.output)
        print(report.human_summary(), end="")
        print(f"JSON report: {json_path}")
        print(f"Text report: {text_path}")
        print(f"Sanitized failures: {failures_path}")
        return 0 if report.thresholds_met else 1
    finally:
        if "container" in locals():
            container.database.engine.dispose()
        logging.shutdown()
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
