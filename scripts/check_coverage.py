from __future__ import annotations

import argparse
import json
from pathlib import Path


def _percentage(files: dict, prefixes: tuple[str, ...]) -> tuple[float, int, int]:
    covered = 0
    total = 0
    for name, report in files.items():
        normalized = name.replace("\\", "/")
        if prefixes and not normalized.startswith(prefixes):
            continue
        summary = report["summary"]
        covered += int(summary["covered_lines"])
        total += int(summary["num_statements"])
    return ((covered / total * 100) if total else 100.0, covered, total)


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce aggregate and critical coverage gates.")
    parser.add_argument("report", type=Path)
    parser.add_argument("--minimum", type=float, default=60.0)
    parser.add_argument("--critical-minimum", type=float, default=80.0)
    args = parser.parse_args()
    payload = json.loads(args.report.read_text("utf-8"))
    files = payload.get("files", {})
    overall, overall_covered, overall_total = _percentage(files, ())
    critical, critical_covered, critical_total = _percentage(
        files,
        (
            "src/assistant/execution/",
            "src/assistant/policy/",
            "src/assistant/scheduling/",
        ),
    )
    print(
        f"Overall line coverage: {overall:.2f}% ({overall_covered}/{overall_total}); "
        f"critical: {critical:.2f}% ({critical_covered}/{critical_total})"
    )
    failures = []
    if overall < args.minimum:
        failures.append(f"overall coverage is below {args.minimum:.2f}%")
    if critical < args.critical_minimum:
        failures.append(f"critical coverage is below {args.critical_minimum:.2f}%")
    if failures:
        raise SystemExit("; ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
