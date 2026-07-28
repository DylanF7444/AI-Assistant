from __future__ import annotations

import pytest

from assistant.router.deterministic import DeterministicRouter


@pytest.mark.parametrize(
    ("text", "skill_id", "arguments"),
    [
        (
            r"Show C:\Users\Example\Pictures\photo.png",
            "media.present",
            {
                "source": r"C:\Users\Example\Pictures\photo.png",
                "autoplay": False,
            },
        ),
        (
            "show https://cdn.example.com/photo.png?token=temporary",
            "media.present",
            {
                "source": "https://cdn.example.com/photo.png?token=temporary",
                "autoplay": False,
            },
        ),
        (
            "pull up the second result",
            "media.present",
            {"source": "second result", "autoplay": False},
        ),
        (
            "play meeting.mp3",
            "media.present",
            {"source": "meeting.mp3", "autoplay": True},
        ),
        (
            "show the vacation photo in Pictures",
            "media.present",
            {
                "query": "vacation",
                "root": "Pictures",
                "kind_hint": "image",
                "autoplay": False,
                "limit": 20,
            },
        ),
        (
            "Pull up an image from my camera roll",
            "media.present",
            {
                "root": "Pictures/Camera Roll",
                "kind_hint": "image",
                "autoplay": False,
                "limit": 20,
            },
        ),
        (
            "play the meeting recording from my Music folder",
            "media.present",
            {
                "query": "meeting",
                "root": "Music",
                "kind_hint": "audio",
                "autoplay": True,
                "limit": 20,
            },
        ),
        ("pause it", "media.viewer_control", {"action": "pause"}),
        ("resume this video", "media.viewer_control", {"action": "play"}),
        (
            "skip ahead thirty seconds",
            "media.viewer_control",
            {"action": "seek_relative", "delta_seconds": 30},
        ),
        (
            "go backward 10 seconds",
            "media.viewer_control",
            {"action": "seek_relative", "delta_seconds": -10},
        ),
        (
            "go to 1 minute 20 seconds",
            "media.viewer_control",
            {"action": "seek_absolute", "position_seconds": 80},
        ),
        (
            "set playback volume to 50 percent",
            "media.viewer_control",
            {"action": "set_volume", "volume": 0.5},
        ),
        (
            "set playback speed to 1.5x",
            "media.viewer_control",
            {"action": "set_rate", "playback_rate": 1.5},
        ),
        ("make it fullscreen", "media.viewer_control", {"action": "fullscreen"}),
        ("close the viewer", "media.viewer_control", {"action": "close"}),
    ],
)
def test_media_requests_route_deterministically(text, skill_id, arguments):
    result = DeterministicRouter().route(text)

    assert not result.needs_llm
    assert result.intent.name == skill_id
    assert result.intent.arguments == arguments


@pytest.mark.parametrize(
    ("text", "expected_skill"),
    [
        ("pause the music", "windows.media"),
        ("play music", "windows.media"),
        ("show me how you work", "planner.goal"),
        ("show me a picture of Saturn", "planner.goal"),
        ("pull up a chair", "planner.goal"),
        ("play some music", "planner.goal"),
        ("show playbook release-check", "core.playbook"),
        ("Show me daily tech deals at 8 am from amazon", "core.schedule"),
    ],
)
def test_media_phrasing_does_not_steal_other_request_families(text, expected_skill):
    result = DeterministicRouter().route(text)

    assert result.intent.name == expected_skill


def test_bare_play_and_pause_do_not_claim_an_in_app_viewer():
    router = DeterministicRouter()

    assert router.route("play").intent.name == "planner.goal"
    assert router.route("pause").intent.name == "planner.goal"
