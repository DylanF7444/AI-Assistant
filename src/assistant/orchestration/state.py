from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from uuid import UUID

from assistant.contracts import MediaDirective


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
    active_presentation_id: UUID | None = None
    last_media_path: str | None = None
    last_media_kind: str | None = None

    def remember_file_candidates(self, paths: tuple[str, ...] | list[str]) -> None:
        self.file_candidates = tuple(str(path) for path in paths)
        if len(self.file_candidates) == 1:
            self.selected_file = self.file_candidates[0]

    def remember_media_directive(self, directive: MediaDirective) -> None:
        self.active_presentation_id = directive.presentation_id
        if directive.source is not None:
            self.last_media_path = directive.source.location
            self.last_media_kind = directive.source.kind.value

    def clear(self) -> None:
        self.session_context.clear()
        self.file_candidates = ()
        self.selected_file = None
        self.pending_clarification = None
        self.active_presentation_id = None
        self.last_media_path = None
        self.last_media_kind = None
