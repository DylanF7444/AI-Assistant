from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from assistant.context.snapshot import ContextSnapshot
from assistant.contracts import Intent, MediaDirective, RequestResultItem
from assistant.language.retrieval import RetrievedContextItem


@dataclass(frozen=True)
class PendingClarificationFollowUp:
    original_text: str
    clarification: str
    folder_name: str | None
    session_id: str
    kind: str = "generic"


@dataclass(frozen=True)
class ReplanningContinuation:
    correlation_id: UUID
    text: str
    current_intent: Intent
    interpretation: Any
    context: ContextSnapshot
    retrieved: tuple[RetrievedContextItem, ...]
    session_id: str
    invoked_by_voice: bool
    observations: tuple[dict[str, Any], ...]
    step_summaries: tuple[str, ...]
    iteration: int
    max_iterations: int


@dataclass
class ConversationState:
    session_context: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=6))
    result_items: tuple[RequestResultItem, ...] = ()
    selected_result_item_id: str | None = None
    result_source: str | None = None
    result_summary: str | None = None
    pending_clarification: PendingClarificationFollowUp | None = None
    active_presentation_id: UUID | None = None
    last_media_path: str | None = None
    last_media_kind: str | None = None

    def remember_result_items(
        self,
        items: tuple[RequestResultItem, ...] | list[RequestResultItem],
        *,
        source: str | None = None,
        summary: str | None = None,
    ) -> None:
        self.result_items = tuple(items)
        self.result_source = source
        self.result_summary = summary
        self.selected_result_item_id = (
            self.result_items[0].item_id if len(self.result_items) == 1 else None
        )

    @property
    def selected_result_item(self) -> RequestResultItem | None:
        if self.selected_result_item_id is None:
            return None
        return next(
            (item for item in self.result_items if item.item_id == self.selected_result_item_id),
            None,
        )

    def select_result_item(self, item: RequestResultItem) -> None:
        if item not in self.result_items:
            raise ValueError("Selected result item must belong to the latest result set")
        self.selected_result_item_id = item.item_id

    @property
    def file_candidates(self) -> tuple[str, ...]:
        return tuple(
            str(item.value)
            for item in self.result_items
            if item.kind == "file" and isinstance(item.value, str)
        )

    @file_candidates.setter
    def file_candidates(self, paths: tuple[str, ...] | list[str]) -> None:
        self.remember_file_candidates(paths)

    @property
    def selected_file(self) -> str | None:
        selected = self.selected_result_item
        if selected is None or selected.kind != "file" or not isinstance(selected.value, str):
            return None
        return selected.value

    @selected_file.setter
    def selected_file(self, path: str | None) -> None:
        if path is None:
            selected = self.selected_result_item
            if selected is not None and selected.kind == "file":
                self.selected_result_item_id = None
            return
        for item in self.result_items:
            if item.kind == "file" and item.value == path:
                self.selected_result_item_id = item.item_id
                return
        self.remember_file_candidates([path])

    def remember_file_candidates(self, paths: tuple[str, ...] | list[str]) -> None:
        items = tuple(
            RequestResultItem(
                item_id=f"file-{index}",
                title=str(path),
                value=str(path),
                kind="file",
                metadata={"path": str(path)},
            )
            for index, path in enumerate(paths, 1)
        )
        self.remember_result_items(
            items,
            source="windows.file_search",
            summary=f"{len(items)} recent file result{'s' if len(items) != 1 else ''}.",
        )

    def remember_media_directive(self, directive: MediaDirective) -> None:
        self.active_presentation_id = directive.presentation_id
        if directive.source is not None:
            self.last_media_path = directive.source.location
            self.last_media_kind = directive.source.kind.value

    def clear(self) -> None:
        self.session_context.clear()
        self.result_items = ()
        self.selected_result_item_id = None
        self.result_source = None
        self.result_summary = None
        self.pending_clarification = None
        self.active_presentation_id = None
        self.last_media_path = None
        self.last_media_kind = None
