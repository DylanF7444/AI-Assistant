import pytest

from assistant.router.deterministic import DeterministicRouter
from assistant.router.normalization import normalize_text, spoken_number


def test_normalization_cleans_spacing_case_and_spoken_punctuation():
    assert normalize_text("  Hello   comma   WORLD  ") == "hello, world"


def test_spoken_number_builds_values_from_words():
    assert spoken_number("two hundred and five") == 205


def test_timer_route_converts_minutes_to_seconds():
    result = DeterministicRouter().route("set a timer for twenty five minutes")

    assert result.intent.name == "core.timer"
    assert result.intent.arguments == {"seconds": 1500, "label": "Timer"}
    assert result.needs_llm is False


def test_unknown_command_is_delegated_to_the_local_planner():
    result = DeterministicRouter().route("organize this project for me")

    assert result.intent.name == "planner.request"
    assert result.intent.ambiguous is True
    assert result.needs_llm is True


@pytest.mark.parametrize(
    ("text", "expected_intent"),
    [
        ("help!", "core.help"),
        ("what time?", "core.datetime"),
        ("calculate 8 divided by 2", "core.calculator"),
        ("volume down", "windows.media"),
    ],
)
def test_common_commands_have_deterministic_routes(text, expected_intent):
    assert DeterministicRouter().route(text).intent.name == expected_intent
