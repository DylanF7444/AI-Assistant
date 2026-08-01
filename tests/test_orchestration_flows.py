from uuid import uuid4

from assistant.contracts import (
    Intent,
    MediaAction,
    MediaDirective,
    RequestResultItem,
    Response,
    ResponseType,
    Transcript,
    WordTimestamp,
)
from assistant.orchestration import ConversationState, FileReferenceResolver, evaluate_voice_gate


def test_conversation_state_owns_file_selection_and_session_reset():
    state = ConversationState()
    state.session_context.append({"request": "hello"})

    state.remember_file_candidates(["report.txt"])
    state.remember_media_directive(
        MediaDirective(
            presentation_id=uuid4(),
            action=MediaAction.PAUSE,
        )
    )

    assert state.file_candidates == ("report.txt",)
    assert state.selected_file == "report.txt"

    state.clear()

    assert not state.session_context
    assert state.file_candidates == ()
    assert state.selected_file is None
    assert state.active_presentation_id is None


def test_conversation_state_owns_generic_result_items_and_selection():
    state = ConversationState()
    first = RequestResultItem(
        item_id="task-1",
        title="First task",
        value={"id": 1},
        kind="task",
    )
    second = RequestResultItem(
        item_id="task-2",
        title="Second task",
        value={"id": 2},
        kind="task",
    )

    state.remember_result_items(
        [first, second],
        source="core.tasks",
        summary="Two tasks.",
    )
    state.select_result_item(second)

    assert state.result_items == (first, second)
    assert state.selected_result_item == second
    assert state.result_source == "core.tasks"
    assert state.file_candidates == ()


def test_file_reference_resolver_returns_typed_clarification():
    clarification = Response(
        display_text="Select a file.",
        response_type=ResponseType.CLARIFICATION,
    )
    resolver = FileReferenceResolver(lambda *args, **kwargs: clarification)

    result = resolver.resolve(
        Intent(name="windows.file_operation", confidence=1, source="rule"),
        session_id="test",
        request_text="delete that file",
    )

    assert result.intent is None
    assert result.clarification == clarification


def test_voice_gate_identifies_uncertain_sensitive_target():
    transcript = Transcript(
        session_id=uuid4(),
        raw_text="delete report",
        normalized_text="delete report",
        confidence=0.7,
        words=(
            WordTimestamp(
                word="report",
                start_seconds=0,
                end_seconds=0.5,
                confidence=0.4,
            ),
        ),
    )
    intent = Intent(
        name="windows.file_operation",
        arguments={"operation": "delete", "source": "report.txt"},
        confidence=1,
        source="rule",
    )

    result = evaluate_voice_gate(transcript, intent)

    assert result.should_clarify
    assert result.sensitive_argument_keys == ("source",)
    assert result.low_confidence_words == transcript.words
