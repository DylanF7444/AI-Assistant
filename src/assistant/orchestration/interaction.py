from __future__ import annotations

from uuid import UUID

from assistant.contracts import Response
from assistant.orchestration.state import ConversationState
from assistant.storage.history import HistoryStore


class InteractionRecorder:
    def __init__(self, history: HistoryStore, state: ConversationState) -> None:
        self.history = history
        self.state = state

    def record(
        self,
        correlation_id: UUID,
        request_text: str,
        response: Response,
        *,
        intent_name: str,
        outcome: str,
        add_session_context: bool = True,
    ) -> None:
        self.history.add(
            correlation_id,
            response.display_text,
            raw_transcript=request_text,
            normalized_transcript=request_text.casefold(),
            intent_name=intent_name,
        )
        if add_session_context:
            self.state.session_context.append(
                {
                    "request": request_text,
                    "intent": intent_name,
                    "outcome": outcome,
                    "response_type": response.response_type.value,
                    "severity": response.severity,
                    "response": response.display_text[:1000],
                }
            )
