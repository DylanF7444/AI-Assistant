from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    value: Any
    sensitivity: Literal["public", "local", "sensitive"] = "local"
    source: str
    summary: str = ""

    def redacted_debug_item(self) -> dict[str, Any]:
        value = "[redacted]" if self.sensitivity == "sensitive" else self.value
        return {
            "key": self.key,
            "title": self.title,
            "sensitivity": self.sensitivity,
            "source": self.source,
            "summary": self.summary,
            "value": value,
        }

    def prompt_item(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "sensitivity": self.sensitivity,
            "source": self.source,
            "summary": self.summary,
            "value": self.value if self.sensitivity != "sensitive" else "[redacted]",
        }


class ContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_text: str
    session_id: str
    items: list[ContextItem] = Field(default_factory=list)

    def keys(self) -> list[str]:
        return [item.key for item in self.items]

    def items_by_key(self) -> dict[str, ContextItem]:
        return {item.key: item for item in self.items}

    def redacted_debug_items(self) -> list[dict[str, Any]]:
        return [item.redacted_debug_item() for item in self.items]

    def prompt_items(self) -> list[dict[str, Any]]:
        return [item.prompt_item() for item in self.items]
