from __future__ import annotations

import asyncio
import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from assistant.contracts import ResultStatus, SkillCall, SkillResult
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.storage.memory import MemoryStore, MemoryType


class MemoryInput(BaseModel):
    operation: Literal["remember", "inspect", "list", "edit", "forget", "delete"]
    type: MemoryType | None = None
    key: str | None = None
    value: str | None = None
    query: str | None = None
    memory_id: int | None = Field(default=None, ge=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    confirm_sensitive: bool = False

    @model_validator(mode="after")
    def _operation_has_required_fields(self) -> MemoryInput:
        if self.operation == "remember" and (not self.type or not self.key or not self.value):
            raise ValueError("remember requires type, key, and value")
        if self.operation == "inspect" and not any((self.memory_id, self.key, self.query)):
            raise ValueError("inspect requires memory_id, key, or query")
        if self.operation == "edit" and not any((self.memory_id, self.key)):
            raise ValueError("edit requires memory_id or key")
        if (
            self.operation == "edit"
            and self.type is None
            and self.value is None
            and self.confidence is None
        ):
            raise ValueError("edit requires type, value, or confidence")
        if self.operation == "forget" and not any((self.memory_id, self.key, self.query)):
            raise ValueError("forget requires memory_id, key, or query")
        if self.operation == "delete" and not any((self.memory_id, self.key)):
            raise ValueError("delete requires memory_id or key")
        return self


class MemoryOutput(BaseModel):
    memories: list[dict]


class MemorySkill(Skill):
    input_model = MemoryInput
    output_model = MemoryOutput

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.manifest = manifest(
            skill_id="core.memory",
            display_name="Memory",
            description="Remember, list, and forget explicit user-approved preferences and facts.",
            input_model=self.input_model,
            output_model=self.output_model,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if args.operation == "remember":
            if _looks_sensitive(args.value or "") and not args.confirm_sensitive:
                return _sensitive_denied()
            item = self.store.remember(
                type=args.type,
                key=args.key,
                value=args.value,
                source="user",
                confidence=1,
            )
            memories = [_memory_dict(item)]
            summary = f"I will remember: {item.value}"
        elif args.operation == "inspect":
            memories = [_memory_dict(item) for item in self._inspect(args)]
            summary = f"{len(memories)} matching memories."
        elif args.operation == "edit":
            if args.value and _looks_sensitive(args.value) and not args.confirm_sensitive:
                return _sensitive_denied()
            item = self.store.update(
                memory_id=args.memory_id,
                key=args.key,
                type=args.type,
                value=args.value,
                confidence=args.confidence,
            )
            memories = [_memory_dict(item)]
            summary = f"Updated memory {item.id}."
        elif args.operation == "forget":
            forgotten = self.store.forget(
                memory_id=args.memory_id,
                key=args.key,
                query=args.query,
            )
            memories = [_memory_dict(item) for item in self.store.list()]
            summary = "Forgot that memory." if forgotten else "I could not find that memory."
        elif args.operation == "delete":
            deleted = self.store.delete(memory_id=args.memory_id, key=args.key)
            memories = [_memory_dict(item) for item in self.store.list()]
            summary = "Deleted that memory." if deleted else "I could not find that memory."
        else:
            values = (
                self.store.search(args.query, type=args.type)
                if args.query
                else self.store.list(type=args.type)
            )
            memories = [_memory_dict(item) for item in values]
            summary = f"{len(memories)} enabled memories."
        display = _memory_display(args.operation, summary, memories)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=summary,
            display_text=display,
            data=MemoryOutput(memories=memories).model_dump(),
            sensitive=True,
        )

    def _inspect(self, args: MemoryInput):
        if args.memory_id is not None:
            item = self.store.get(args.memory_id)
            return (item,) if item is not None and item.enabled else ()
        if args.key:
            return tuple(item for item in self.store.list() if item.key == _clean_key(args.key))
        return self.store.search(args.query or "", type=args.type)


def _memory_dict(item) -> dict:
    return {
        "id": item.id,
        "type": item.type,
        "key": item.key,
        "value": item.value,
        "source": item.source,
        "confidence": item.confidence,
        "enabled": item.enabled,
    }


def _memory_display(operation: str, summary: str, memories: list[dict]) -> str:
    if operation not in {"list", "inspect"}:
        return summary
    if not memories:
        return "No enabled memories matched."
    return summary + "\nEnabled memories:\n" + "\n".join(
        f"- {item['id']} [{item['type']}] {item['key']}: {item['value']}"
        for item in memories
    )


def _looks_sensitive(value: str) -> bool:
    folded = value.casefold()
    if re.search(r"\b(password|passcode|secret|token|api key|private key)\b", folded):
        return True
    if re.search(r"\b\d{3}-\d{2}-\d{4}\b", value):
        return True
    return bool(re.search(r"\b(?:\d[ -]*?){13,19}\b", value))


def _sensitive_denied() -> SkillResult:
    return SkillResult(
        status=ResultStatus.DENIED,
        summary=(
            "That memory looks sensitive. Repeat the request with sensitive "
            "memory confirmation before I store it."
        ),
        data=MemoryOutput(memories=[]).model_dump(),
        sensitive=True,
    )


def _clean_key(value: str) -> str:
    key = "_".join(re.findall(r"[a-z0-9]+", value.casefold()))
    if not key:
        raise ValueError("memory key is required")
    return key[:200]
