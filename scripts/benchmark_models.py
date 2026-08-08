from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import time
import urllib.request
import wave
from pathlib import Path

from assistant.contracts import AudioSession, Response
from assistant.evaluation.speech import (
    SpeechCorpusUnavailable,
    load_speech_corpus,
    percentile,
    speech_condition_summary,
    word_error_rate,
)
from assistant.observability import process_working_set_bytes
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


async def bytes_from_file(path: Path, chunk_milliseconds: int = 20):
    with wave.open(str(path), "rb") as audio:
        frame_count = max(1, int(audio.getframerate() * chunk_milliseconds / 1_000))
        while payload := audio.readframes(frame_count):
            yield payload
            await asyncio.sleep(len(payload) / (audio.getframerate() * audio.getnchannels() * 2))


async def benchmark_stt(
    model: str,
    corpus: Path,
    *,
    assets_root: Path | None = None,
    device: str = "auto",
) -> dict:
    resolved_corpus = load_speech_corpus(corpus, asset_root=assets_root)
    provider = FasterWhisperProvider(model, device)
    memory_before = process_working_set_bytes()
    load_started = time.perf_counter()
    await provider.warmup()
    load_seconds = time.perf_counter() - load_started
    memory_after_load = process_working_set_bytes()
    samples = []
    for resolved in resolved_corpus.cases:
        case = resolved.case
        path = resolved.path
        with wave.open(str(path), "rb") as audio:
            session = AudioSession(
                source=str(path),
                sample_rate=audio.getframerate(),
                channels=audio.getnchannels(),
            )
            duration = resolved.duration_seconds
        started = time.perf_counter()
        transcripts = []
        transcript_times = []
        async for item in provider.transcribe(session, bytes_from_file(path)):
            transcripts.append(item)
            transcript_times.append(time.perf_counter() - started)
        elapsed = time.perf_counter() - started
        hypothesis = transcripts[-1].raw_text if transcripts else ""
        partial_times = [
            timestamp
            for item, timestamp in zip(transcripts, transcript_times, strict=True)
            if not item.is_final
        ]
        samples.append(
            {
                "case_id": case.case_id,
                "condition": case.condition,
                "audio": case.audio,
                "duration_seconds": duration,
                "real_time_factor": elapsed / duration,
                "processing_overhang_seconds": max(0.0, elapsed - duration),
                "first_partial_seconds": partial_times[0] if partial_times else None,
                "final_seconds": max(0.0, transcript_times[-1] - duration)
                if transcript_times
                else elapsed,
                "word_error_rate": word_error_rate(case.reference, hypothesis),
                "false_activation": case.expected_behavior == "silence"
                and bool(hypothesis.strip()),
                "confidence": (transcripts[-1].confidence if transcripts else None),
            }
        )
    condition_summaries = speech_condition_summary(samples, resolved_corpus.corpus.thresholds)
    first_partials = [
        float(item["first_partial_seconds"])
        for item in samples
        if item["first_partial_seconds"] is not None
    ]
    finals = [float(item["final_seconds"]) for item in samples]
    false_activations = [item for item in samples if item["false_activation"]]
    return {
        "model": model,
        "requested_device": device,
        "provider_health": await provider.health(),
        "corpus_id": resolved_corpus.corpus.corpus_id,
        "corpus_version": resolved_corpus.corpus.version,
        "corpus_digest": resolved_corpus.digest,
        "load_seconds": load_seconds,
        "working_set_before_bytes": memory_before,
        "working_set_after_load_bytes": memory_after_load,
        "working_set_load_delta_bytes": (
            memory_after_load - memory_before
            if memory_before is not None and memory_after_load is not None
            else None
        ),
        "mean_real_time_factor": statistics.mean(item["real_time_factor"] for item in samples),
        "mean_word_error_rate": statistics.mean(item["word_error_rate"] for item in samples),
        "first_partial_p50_seconds": percentile(first_partials, 0.50),
        "first_partial_p95_seconds": percentile(first_partials, 0.95),
        "final_p50_seconds": percentile(finals, 0.50),
        "final_p95_seconds": percentile(finals, 0.95),
        "false_activation_rate": len(false_activations)
        / max(1, len([item for item in samples if item["condition"] == "silence"])),
        "conditions": condition_summaries,
        "thresholds_passed": all(
            summary["word_error_rate_passed"] for summary in condition_summaries.values()
        ),
        "samples": samples,
    }


async def benchmark_tts(executable: Path, model: Path) -> dict:
    provider = PiperProvider(executable, model)
    phrases = (
        "Done.",
        "The local assistant is ready.",
        "I found three matching documents. The first result is the quarterly launch brief.",
    )
    samples = []
    for run in ("cold", "warm"):
        for phrase in phrases:
            response = Response(display_text="Benchmark", speech_text=phrase, speak=True)
            started = time.perf_counter()
            first_event = None
            async for _event in provider.speak(response):
                first_event = first_event or time.perf_counter() - started
            samples.append(
                {
                    "run": run,
                    "characters": len(phrase),
                    "first_audio_seconds": first_event,
                    "completion_seconds": time.perf_counter() - started,
                }
            )
    first_audio = [
        float(item["first_audio_seconds"])
        for item in samples
        if item["first_audio_seconds"] is not None
    ]
    return {
        "model": str(model),
        "first_audio_p50_seconds": percentile(first_audio, 0.50),
        "first_audio_p95_seconds": percentile(first_audio, 0.95),
        "first_audio_target_seconds": 0.75,
        "first_audio_target_passed": bool(first_audio) and max(first_audio) <= 0.75,
        "samples": samples,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-model")
    parser.add_argument("--stt-model")
    parser.add_argument("--stt-device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--audio-corpus", type=Path)
    parser.add_argument("--audio-assets-root", type=Path)
    parser.add_argument("--validate-speech-corpus", action="store_true")
    parser.add_argument("--piper-executable", type=Path)
    parser.add_argument("--piper-model", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/models.json"))
    args = parser.parse_args()
    report = {
        "generated_at": time.time(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
        },
        "results": {},
    }
    if args.llm_model:
        report["results"]["llm"] = await asyncio.to_thread(benchmark_llm, args.llm_model)
    exit_code = 0
    if args.audio_corpus and (args.stt_model or args.validate_speech_corpus):
        try:
            if args.stt_model:
                report["results"]["stt"] = await benchmark_stt(
                    args.stt_model,
                    args.audio_corpus,
                    assets_root=args.audio_assets_root,
                    device=args.stt_device,
                )
            else:
                corpus = load_speech_corpus(args.audio_corpus, asset_root=args.audio_assets_root)
                report["results"]["speech_corpus"] = {
                    "status": "ready",
                    "corpus_id": corpus.corpus.corpus_id,
                    "version": corpus.corpus.version,
                    "digest": corpus.digest,
                    "case_count": len(corpus.cases),
                }
        except SpeechCorpusUnavailable as exc:
            report["results"]["speech_corpus"] = {
                "status": "blocked",
                "problems": list(exc.problems),
            }
            exit_code = 2
    if args.piper_executable and args.piper_model:
        report["results"]["tts"] = await benchmark_tts(args.piper_executable, args.piper_model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(args.output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
