from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import urllib.request
import wave
from pathlib import Path

from assistant.contracts import AudioSession, Response
from assistant.stt.faster_whisper import FasterWhisperProvider
from assistant.tts.piper import PiperProvider

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {"type": "string"},
        "arguments": {"type": "object"},
    },
    "required": ["skill_id", "arguments"],
    "additionalProperties": False,
}

LLM_CASES = [
    (
        "Set a timer for five minutes",
        "core.timer",
        {"seconds": 300},
    ),
    (
        "Calculate 17 times 19",
        "core.calculator",
        {"expression": "17 * 19"},
    ),
    (
        "What time is it?",
        "core.datetime",
        {"query": "time"},
    ),
]


def benchmark_llm(model: str) -> dict:
    samples = []
    correct = 0
    for request, expected_skill, expected_arguments in LLM_CASES:
        prompt = (
            "Return the single matching registered skill as JSON. "
            "Available skills and exact argument schemas: "
            'core.timer={"seconds":number}; '
            'core.calculator={"expression":string}; '
            'core.datetime={"query":"date"|"time"|"both"}. '
            "Do not invent argument names. "
            f"Request: {request}"
        )
        body = json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": TOOL_SCHEMA,
                "options": {"temperature": 0, "num_predict": 128},
            }
        ).encode()
        started = time.perf_counter()
        with urllib.request.urlopen(
            urllib.request.Request(
                "http://127.0.0.1:11434/api/generate",
                data=body,
                headers={"Content-Type": "application/json"},
            ),
            timeout=120,
        ) as response:
            payload = json.load(response)
        elapsed = time.perf_counter() - started
        try:
            output = json.loads(payload["response"])
        except json.JSONDecodeError:
            output = {}
        arguments = output.get("arguments", {})
        matched = output.get("skill_id") == expected_skill and all(
            (
                str(arguments.get(key, "")).replace(" ", "") == str(value).replace(" ", "")
                if key == "expression"
                else arguments.get(key) == value
            )
            for key, value in expected_arguments.items()
        )
        correct += int(matched)
        eval_count = payload.get("eval_count", 0)
        eval_duration = payload.get("eval_duration", 0)
        samples.append(
            {
                "request": request,
                "seconds": elapsed,
                "tokens_per_second": (
                    eval_count / (eval_duration / 1_000_000_000)
                    if eval_count and eval_duration
                    else None
                ),
                "correct": matched,
                "output": output,
            }
        )
    return {
        "model": model,
        "tool_call_accuracy": correct / len(samples),
        "median_latency_seconds": statistics.median(item["seconds"] for item in samples),
        "samples": samples,
    }


async def bytes_from_file(path: Path, chunk_size: int = 32000):
    with wave.open(str(path), "rb") as audio:
        while payload := audio.readframes(chunk_size // 2):
            yield payload


def word_error_rate(reference: str, hypothesis: str) -> float:
    reference_words = reference.casefold().split()
    hypothesis_words = hypothesis.casefold().split()
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


async def benchmark_stt(model: str, corpus: Path) -> dict:
    provider = FasterWhisperProvider(model)
    cases = json.loads(corpus.read_text("utf-8"))
    samples = []
    for case in cases:
        path = (corpus.parent / case["audio"]).resolve()
        with wave.open(str(path), "rb") as audio:
            session = AudioSession(
                source=str(path),
                sample_rate=audio.getframerate(),
                channels=audio.getnchannels(),
            )
            duration = audio.getnframes() / audio.getframerate()
        started = time.perf_counter()
        transcripts = [item async for item in provider.transcribe(session, bytes_from_file(path))]
        elapsed = time.perf_counter() - started
        hypothesis = transcripts[-1].raw_text if transcripts else ""
        samples.append(
            {
                "audio": case["audio"],
                "real_time_factor": elapsed / duration,
                "word_error_rate": word_error_rate(case["reference"], hypothesis),
                "confidence": (transcripts[-1].confidence if transcripts else None),
            }
        )
    return {
        "model": model,
        "mean_real_time_factor": statistics.mean(item["real_time_factor"] for item in samples),
        "mean_word_error_rate": statistics.mean(item["word_error_rate"] for item in samples),
        "samples": samples,
    }


async def benchmark_tts(executable: Path, model: Path) -> dict:
    provider = PiperProvider(executable, model)
    response = Response(
        display_text="Benchmark",
        speech_text="The local assistant is ready.",
        speak=True,
    )
    started = time.perf_counter()
    first_event = None
    async for _event in provider.speak(response):
        first_event = time.perf_counter() - started
    return {"model": str(model), "first_audio_seconds": first_event}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-model")
    parser.add_argument("--stt-model")
    parser.add_argument("--audio-corpus", type=Path)
    parser.add_argument("--piper-executable", type=Path)
    parser.add_argument("--piper-model", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/models.json"))
    args = parser.parse_args()
    report = {"generated_at": time.time(), "results": {}}
    if args.llm_model:
        report["results"]["llm"] = await asyncio.to_thread(benchmark_llm, args.llm_model)
    if args.stt_model and args.audio_corpus:
        report["results"]["stt"] = await benchmark_stt(args.stt_model, args.audio_corpus)
    if args.piper_executable and args.piper_model:
        report["results"]["tts"] = await benchmark_tts(args.piper_executable, args.piper_model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
