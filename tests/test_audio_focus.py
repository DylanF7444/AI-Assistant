from assistant.audio.focus import AudioFocusCoordinator


def test_audio_focus_keeps_capture_suppressed_until_every_output_stops():
    aggregate_states: list[bool] = []
    focus = AudioFocusCoordinator(aggregate_states.append)
    media = focus.output_callback("media_viewer")
    tts = focus.output_callback("tts")

    media(True)
    tts(True)
    media(False)

    assert focus.output_active is True
    assert focus.active_outputs == ("tts",)
    assert aggregate_states == [True]

    tts(False)

    assert focus.output_active is False
    assert focus.active_outputs == ()
    assert aggregate_states == [True, False]


def test_audio_focus_updates_are_idempotent_and_clear_is_safe():
    aggregate_states: list[bool] = []
    focus = AudioFocusCoordinator(aggregate_states.append)

    focus.set_output_active("tts", True)
    focus.set_output_active("tts", True)
    focus.set_output_active("media_viewer", True)
    focus.set_output_active("tts", False)
    focus.clear()
    focus.clear()

    assert aggregate_states == [True, False]
    assert focus.active_outputs == ()


def test_audio_focus_rejects_an_unnamed_source():
    focus = AudioFocusCoordinator()

    try:
        focus.set_output_active(" ", True)
    except ValueError as error:
        assert "name" in str(error)
    else:
        raise AssertionError("Expected an unnamed audio source to be rejected")
