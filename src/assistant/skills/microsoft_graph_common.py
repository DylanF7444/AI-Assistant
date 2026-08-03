from __future__ import annotations

import hashlib
from typing import Any

from assistant.integrations.microsoft_graph import next_page_token


def resources_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("value")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return [payload] if payload else []


def bounded_string(value: Any, limit: int = 2048) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def continuation_token(payload: dict[str, Any]) -> str | None:
    return next_page_token(payload)


def short_id(item: dict[str, Any]) -> str:
    raw = str(item.get("id") or "item").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:10]


def without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
