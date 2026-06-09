from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from assistant.storage.database import AuditStore


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("correlation_id", "stage", "skill_id", "outcome"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(AuditStore.redact(payload), separators=(",", ":"))


def configure_logging(data_dir: Path, *, developer_mode: bool = False) -> Path:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    destination = log_dir / "assistant.jsonl"
    root = logging.getLogger("assistant")
    root.setLevel(logging.DEBUG if developer_mode else logging.INFO)
    for handler in tuple(root.handlers):
        root.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        destination, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.propagate = False
    return destination
