from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PendingClarificationFollowUp:
    original_text: str
    clarification: str
    folder_name: str | None
    session_id: str
    kind: str = "generic"


@dataclass
class ConversationState:
    session_context: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=6))
    file_candidates: tuple[str, ...] = ()
    selected_file: str | None = None
    pending_clarification: PendingClarificationFollowUp | None = None

    def remember_file_candidates(self, paths: tuple[str, ...] | list[str]) -> None:
        self.file_candidates = tuple(str(path) for path in paths)
        if len(self.file_candidates) == 1:
            self.selected_file = self.file_candidates[0]

    def clear(self) -> None:
        self.session_context.clear()
        self.file_candidates = ()
        self.selected_file = None
        self.pending_clarification = None
