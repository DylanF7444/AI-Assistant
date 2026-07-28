from __future__ import annotations

import json
import platform
import shutil
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assistant.audio.capture import enumerate_devices
from assistant.config import Settings
from assistant.presentation.remote_media import RemoteMediaCache
from assistant.presentation.session import MediaSessionRegistry
from assistant.skills.registry import SkillRegistry
from assistant.storage.database import Database


def multimedia_report(
    *,
    media_cache: RemoteMediaCache | None = None,
    media_sessions: MediaSessionRegistry | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "available": False,
        "image_formats": (),
        "audio_video_formats": (),
        "output_devices": (),
        "cache_bytes": media_cache.cache_size() if media_cache is not None else 0,
        "last_error_code": None,
    }
    if media_sessions is not None:
        snapshot = media_sessions.current()
        if snapshot is not None:
            report["last_error_code"] = snapshot.error_code
    try:
        from PySide6.QtGui import QImageReader
        from PySide6.QtMultimedia import QMediaDevices

        from assistant.ui.desktop.media_backend import QtMediaPlaybackBackend

        report.update(
            available=True,
            image_formats=tuple(
                sorted(
                    bytes(value).decode("ascii", errors="ignore").casefold()
                    for value in QImageReader.supportedImageFormats()
                )
            ),
            audio_video_formats=QtMediaPlaybackBackend.supported_formats(),
            output_devices=tuple(
                sorted(
                    {
                        device.description()
                        for device in QMediaDevices.audioOutputs()
                        if device.description()
                    }
                )
            ),
        )
    except Exception as error:
        report["backend_error_type"] = type(error).__name__
    return report


def hardware_report(
    *,
    media_cache: RemoteMediaCache | None = None,
    media_sessions: MediaSessionRegistry | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "audio_devices": len(enumerate_devices()),
        "cuda_available": False,
        "gpu": None,
        "multimedia": multimedia_report(
            media_cache=media_cache,
            media_sessions=media_sessions,
        ),
    }
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        completed = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode == 0:
            report["cuda_available"] = True
            report["gpu"] = completed.stdout.strip()
    return report


def model_recommendations(report: dict[str, Any]) -> dict[str, str]:
    return {
        "stt": "small.en" if report["cuda_available"] else "base.en",
        "llm": "3B Q4 on GPU/CPU hybrid" if report["cuda_available"] else "3B Q4 CPU",
        "tts": "Piper medium voice",
    }


def diagnostic_bundle(
    destination: Path,
    settings: Settings,
    database: Database,
    registry: SkillRegistry,
) -> Path:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "hardware": hardware_report(),
        "recommendations": model_recommendations(hardware_report()),
        "settings": settings.model_dump(
            mode="json",
            exclude={"data_dir", "trusted_model_keys"},
        ),
        "database_integrity": database.integrity_check(),
        "skills": [
            {
                "id": item.skill_id,
                "version": item.version,
                "api_version": item.api_version,
                "risk": item.risk,
            }
            for item in registry.list()
        ],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps(payload, indent=2, default=str))
    return destination
