from __future__ import annotations

import asyncio
import json
import statistics
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.diagnostics import hardware_report


async def run(iterations: int = 100) -> dict:
    settings = Settings(profile="testing", data_dir=Path(".benchmark-data"))
    container = build_container(settings)
    samples = []
    tracemalloc.start()
    for _ in range(iterations):
        started = time.perf_counter()
        await container.orchestrator.handle_text("calculate 17 times 19")
        samples.append((time.perf_counter() - started) * 1000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "iterations": iterations,
        "deterministic_command_ms": {
            "median": statistics.median(samples),
            "p95": sorted(samples)[int(len(samples) * 0.95) - 1],
            "max": max(samples),
        },
        "python_peak_bytes": peak,
        "hardware": hardware_report(),
        "limitations": [
            "This benchmark excludes microphone, STT, local LLM, and TTS model latency.",
            "Model benchmarks require installed local model files and controlled audio.",
        ],
    }


if __name__ == "__main__":
    output = asyncio.run(run())
    destination = Path("benchmarks") / "latest.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(destination)
