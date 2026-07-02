import pytest

from assistant.audio.activation import (
    ClapDetector,
    keyword_request,
    parse_voice_prompt_response,
)
from assistant.config import Settings


def test_keyword_request_extracts_inline_request():
    assert (
        keyword_request("Assistant, calculate one plus one", "assistant")
        == "calculate one plus one"
    )


def test_keyword_request_allows_keyword_only_activation():
    assert keyword_request("hey assistant", "hey assistant") == ""


def test_clap_detector_requires_transient_spike():
    detector = ClapDetector(threshold=0.35)
    assert detector.observe(0.02) is False
    assert detector.observe(0.5) is True
    assert detector.observe(0.55) is False


def test_keyword_clap_mode_requires_a_trigger(tmp_path):
    with pytest.raises(ValueError, match="keyword or clap trigger"):
        Settings(
            data_dir=tmp_path,
            voice_activation_mode="keyword_clap",
            voice_activation_keyword_enabled=False,
            voice_activation_clap_enabled=False,
        )


def test_voice_prompt_response_approves_and_denies():
    assert parse_voice_prompt_response("yes continue", "approval").action == "approve"
    assert parse_voice_prompt_response("no not now", "approval").action == "deny"


def test_voice_prompt_response_selects_permission_duration():
    assert (
        parse_voice_prompt_response("grant just once", "permission_duration").action
        == "once"
    )
    assert (
        parse_voice_prompt_response("for this session", "permission_duration").action
        == "session"
    )
    assert (
        parse_voice_prompt_response("always remember this", "permission_duration").action
        == "always"
    )


def test_voice_prompt_response_selects_permission_targets():
    assert (
        parse_voice_prompt_response("only the requested targets", "permission_targets").action
        == "requested_targets"
    )
    assert (
        parse_voice_prompt_response("allow any target", "permission_targets").action
        == "any_target"
    )


def test_voice_prompt_response_matches_spoken_critical_token():
    response = parse_voice_prompt_response(
        "one two a b c six", "critical_token", expected_token="12abc6"
    )
    assert response is not None
    assert response.action == "token"
    assert response.value == "12abc6"
