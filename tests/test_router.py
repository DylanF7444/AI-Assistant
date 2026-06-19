import pytest

from assistant.router.deterministic import DeterministicRouter
from assistant.router.normalization import normalize_text, spoken_number


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("cancel", "control.cancel"),
        ("help", "core.help"),
        ("what time", "core.datetime"),
        ("what is the date", "core.datetime"),
        ("calculate 2 plus 3", "core.calculator"),
        ("play music", "windows.media"),
        ("volume up", "windows.media"),
        ("terminate process 123", "windows.process"),
        ("run macro morning", "core.macro"),
        ("forget this session", "control.forget"),
        ("correct that to calculate 3 plus 4", "control.correct"),
        ("help!", "core.help"),
        ("what time?", "core.datetime"),
        ("calculate 2 plus 3.", "core.calculator"),
        (
            "How do I register an application so you can open it for me",
            "conversation.answer",
        ),
    ],
)
def test_deterministic_routes(text, name):
    result = DeterministicRouter().route(text)
    assert result.intent.name == name
    assert not result.needs_llm


def test_timer_normalizes_spoken_number():
    result = DeterministicRouter().route("set a timer for twenty five minutes")
    assert result.intent.arguments["seconds"] == 1500


def test_unknown_request_needs_local_planner():
    result = DeterministicRouter().route("organize these files for me")
    assert result.needs_llm
    assert result.intent.ambiguous


def test_application_registration_help_does_not_need_local_planner():
    result = DeterministicRouter().route(
        "How do I register an application so you can open it for me"
    )
    assert not result.needs_llm
    assert "Applications tab" in result.intent.arguments["text"]
    assert "open <name>" in result.intent.arguments["text"]


def test_knowledge_question_is_not_misrouted_to_calculator():
    result = DeterministicRouter().route("What is the capital of France?")
    assert result.needs_llm
    assert result.intent.name == "conversation.question"


def test_normalization_and_numbers():
    assert normalize_text("Hello comma WORLD") == "hello, world"
    assert spoken_number("one hundred and five") == 105
