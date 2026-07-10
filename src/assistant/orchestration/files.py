from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from assistant.contracts import Intent, Response


@dataclass(frozen=True)
class FileResolution:
    intent: Intent | None = None
    clarification: Response | None = None

    @classmethod
    def from_value(cls, value: Intent | Response) -> FileResolution:
        if isinstance(value, Response):
            return cls(clarification=value)
        return cls(intent=value)


class FileReferenceResolver:
    def __init__(
        self,
        resolve: Callable[..., Intent | Response],
    ) -> None:
        self._resolve = resolve

    def resolve(
        self,
        intent: Intent,
        *,
        session_id: str,
        request_text: str,
    ) -> FileResolution:
        return FileResolution.from_value(
            self._resolve(
                intent,
                session_id=session_id,
                request_text=request_text,
            )
        )
