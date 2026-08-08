from __future__ import annotations

import ctypes
import math
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum

type MetricDimensionValue = str | int | float | bool


class MetricStage(StrEnum):
    ACTIVATION_RECEIVED = "activation.received"
    LISTENING_VISIBLE = "ui.listening_visible"
    AUDIO_FIRST_BLOCK = "audio.first_block"
    AUDIO_END_OF_UTTERANCE = "audio.end_of_utterance"
    STT_FIRST_PARTIAL = "stt.first_partial"
    STT_FINAL = "stt.final"
    ROUTING = "routing"
    PLANNER_RETRIEVAL = "planner.retrieval"
    PLANNER_CONTEXT = "planner.context"
    PLANNER_GROUNDED = "planner.grounded"
    PLANNER_TOTAL = "planner.total"
    POLICY_PREPARE = "policy.prepare"
    PERMISSION_WAIT = "policy.permission_wait"
    CONFIRMATION_WAIT = "policy.confirmation_wait"
    EXECUTION = "execution"
    RESPONSE_FIRST_VISIBLE = "response.first_visible"
    TTS_SYNTHESIS = "tts.synthesis"
    TTS_FIRST_AUDIO = "tts.first_audio"
    TTS_PLAYBACK = "tts.playback"
    TTS_COMPLETE = "tts.complete"
    REQUEST_TOTAL = "request.total"
    RESOURCE_MEMORY = "resource.memory"
    RESOURCE_QUEUE_DEPTH = "resource.queue_depth"


def process_working_set_bytes() -> int:
    """Return current resident process memory without an optional dependency."""

    if os.name == "nt":
        try:

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("page_fault_count", ctypes.c_ulong),
                    ("peak_working_set_size", ctypes.c_size_t),
                    ("working_set_size", ctypes.c_size_t),
                    ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                    ("quota_paged_pool_usage", ctypes.c_size_t),
                    ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                    ("quota_non_paged_pool_usage", ctypes.c_size_t),
                    ("pagefile_usage", ctypes.c_size_t),
                    ("peak_pagefile_usage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                process,
                ctypes.byref(counters),
                counters.cb,
            ):
                return int(counters.working_set_size)
        except (AttributeError, OSError, TypeError):
            return 0
    return 0


class InteractionMode(StrEnum):
    TYPED = "typed"
    VOICE = "voice"
    BACKGROUND = "background"
    SYSTEM = "system"


class RequestPath(StrEnum):
    DETERMINISTIC = "deterministic"
    PLANNER = "planner"
    EXECUTION = "execution"
    SPEECH = "speech"


@dataclass(frozen=True)
class MetricDimensions:
    interaction: InteractionMode | None = None
    path: RequestPath | None = None
    provider: str | None = None
    model: str | None = None
    device: str | None = None
    warm: bool | None = None

    def values(self) -> dict[str, MetricDimensionValue]:
        return normalize_metric_dimensions(
            {key: value for key, value in asdict(self).items() if value is not None}
        )


_DIMENSION_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
_ALLOWED_DIMENSIONS = {"interaction", "path", "provider", "model", "device", "warm"}


def normalize_metric_dimensions(
    dimensions: MetricDimensions | Mapping[str, MetricDimensionValue] | None,
) -> dict[str, MetricDimensionValue]:
    if dimensions is None:
        return {}
    if isinstance(dimensions, MetricDimensions):
        return dimensions.values()
    normalized: dict[str, MetricDimensionValue] = {}
    for raw_key, raw_value in dimensions.items():
        key = str(raw_key).strip().casefold()
        if key not in _ALLOWED_DIMENSIONS or _DIMENSION_KEY.fullmatch(key) is None:
            raise ValueError(f"Unsupported metric dimension: {raw_key}")
        value: MetricDimensionValue
        if isinstance(raw_value, StrEnum):
            value = str(raw_value)
        elif isinstance(raw_value, bool | int | float | str):
            value = raw_value
        else:
            raise TypeError(f"Unsupported value for metric dimension {key}")
        if isinstance(value, str):
            value = " ".join(value.split())
            if not value or len(value) > 120:
                raise ValueError(f"Invalid value for metric dimension {key}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Metric dimension {key} must be finite")
        normalized[key] = value
    return normalized
