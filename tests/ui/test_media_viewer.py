from __future__ import annotations

import base64
from uuid import uuid4

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QWidget

from assistant.audio.focus import AudioFocusCoordinator
from assistant.bootstrap import build_container
from assistant.contracts import (
    MediaAction,
    MediaDirective,
    MediaKind,
    MediaSource,
    Response,
    ResponseType,
)
from assistant.presentation.media_probe import MediaProbe
from assistant.presentation.session import (
    MediaSessionRegistry,
    MediaSessionStatus,
)
from assistant.storage.database import SettingsStore
from assistant.ui.desktop.app import run_desktop
from assistant.ui.desktop.media_backend import (
    MediaPlaybackBackend,
    QtMediaPlaybackBackend,
)
from assistant.ui.desktop.media_viewer import MediaCompactCard, MediaViewer
from assistant.ui.desktop.state import DesktopWindowState

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def prepared_image(tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(_PNG)
    sessions = MediaSessionRegistry()
    source = MediaProbe().inspect(path)
    snapshot = sessions.prepare(source)
    directive = MediaDirective(
        presentation_id=snapshot.presentation_id,
        action=MediaAction.SHOW,
        source=source,
    )
    return path, sessions, source, directive


class FakeMediaBackend(MediaPlaybackBackend):
    def __init__(self) -> None:
        super().__init__()
        self.video_widget = QWidget()
        self.calls: list[tuple] = []
        self.position = 0.0
        self.volume = 1.0
        self.muted = False

    def load(
        self,
        source: MediaSource,
        *,
        autoplay: bool,
        start_seconds: float | None,
    ) -> None:
        self.calls.append(("load", source.kind, autoplay, start_seconds))
        self.state_changed.emit(MediaSessionStatus.LOADING)
        self.duration_changed.emit(120.0)
        self.position = start_seconds or 0.0
        self.position_changed.emit(self.position)
        self.volume_changed.emit(self.volume, self.muted)
        self.state_changed.emit(MediaSessionStatus.READY)
        if autoplay:
            self.play()

    def play(self) -> None:
        self.calls.append(("play",))
        self.state_changed.emit(MediaSessionStatus.PLAYING)

    def pause(self) -> None:
        self.calls.append(("pause",))
        self.state_changed.emit(MediaSessionStatus.PAUSED)

    def stop(self) -> None:
        self.calls.append(("stop",))
        self.position = 0.0
        self.position_changed.emit(0.0)
        self.state_changed.emit(MediaSessionStatus.STOPPED)

    def seek(self, seconds: float) -> None:
        self.position = max(0.0, min(120.0, seconds))
        self.calls.append(("seek", self.position))
        self.position_changed.emit(self.position)

    def set_volume(self, volume: float) -> None:
        self.volume = min(1.0, max(0.0, volume))
        self.calls.append(("volume", self.volume))
        self.volume_changed.emit(self.volume, self.muted)

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        self.calls.append(("muted", muted))
        self.volume_changed.emit(self.volume, self.muted)

    def set_rate(self, rate: float) -> None:
        self.calls.append(("rate", rate))

    def clear(self) -> None:
        self.calls.append(("clear",))

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))


def prepared_av(tmp_path, kind: MediaKind):
    sessions = MediaSessionRegistry()
    if kind == MediaKind.AUDIO:
        path = tmp_path / "sample.wav"
        path.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + (b"\x00" * 28))
    else:
        path = tmp_path / "sample.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomvide")
    source = MediaProbe().inspect(path)
    snapshot = sessions.prepare(source)
    directive = MediaDirective(
        presentation_id=snapshot.presentation_id,
        action=MediaAction.SHOW,
        source=source,
        position_seconds=2,
    )
    return path, sessions, source, snapshot, directive


@pytest.mark.ui
def test_image_viewer_loads_zooms_rotates_and_closes(qtbot, tmp_path):
    _path, sessions, _source, directive = prepared_image(tmp_path)
    viewer = MediaViewer(sessions)
    qtbot.addWidget(viewer)

    viewer.apply_directive(directive)
    qtbot.waitUntil(
        lambda: sessions.current().status == MediaSessionStatus.READY,
        timeout=3000,
    )

    assert viewer.stack.currentWidget() is viewer.image_page
    assert viewer.image_canvas.has_image
    assert viewer.status.text() == "IMAGE"
    viewer.image_canvas.actual_size()
    viewer.image_canvas.zoom_by(1.2)
    assert viewer.image_canvas.scale_value == pytest.approx(1.2)
    viewer.image_canvas.rotate_by(90)
    assert viewer.image_canvas._item.rotation() == 90

    with qtbot.waitSignal(viewer.fullscreen_requested, timeout=1000) as signal:
        viewer.toggle_fullscreen()
    assert signal.args == [True]
    assert sessions.current().fullscreen is True

    viewer.clear()
    assert sessions.current().status == MediaSessionStatus.CLOSED
    assert viewer.isHidden()
    viewer.shutdown()


@pytest.mark.ui
def test_image_viewer_rejects_changed_file_before_decode(qtbot, tmp_path):
    path, sessions, _source, directive = prepared_image(tmp_path)
    path.write_bytes(_PNG + b"changed")
    viewer = MediaViewer(sessions)
    qtbot.addWidget(viewer)

    viewer.apply_directive(directive)
    qtbot.waitUntil(
        lambda: sessions.current().status == MediaSessionStatus.FAILED,
        timeout=3000,
    )

    assert viewer.stack.currentWidget() is viewer.error_page
    assert "changed" in viewer.error_page.detail.text().casefold()
    assert sessions.current().error_code == "media_validation_failed"
    viewer.shutdown()


@pytest.mark.ui
def test_stale_image_callback_cannot_replace_newer_presentation(qtbot, tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(_PNG)
    second.write_bytes(_PNG)
    sessions = MediaSessionRegistry()
    first_source = MediaProbe().inspect(first)
    first_snapshot = sessions.prepare(first_source)
    first_directive = MediaDirective(
        presentation_id=first_snapshot.presentation_id,
        action=MediaAction.SHOW,
        source=first_source,
    )
    second_source = MediaProbe().inspect(second)
    second_snapshot = sessions.prepare(second_source)
    second_directive = MediaDirective(
        presentation_id=second_snapshot.presentation_id,
        action=MediaAction.SHOW,
        source=second_source,
    )
    viewer = MediaViewer(sessions)
    qtbot.addWidget(viewer)

    viewer.apply_directive(first_directive)
    viewer.apply_directive(second_directive)
    qtbot.waitUntil(
        lambda: sessions.current().status == MediaSessionStatus.READY,
        timeout=3000,
    )

    assert viewer.title.text() == "second.png"
    assert viewer._active_presentation_id == second_snapshot.presentation_id
    viewer.shutdown()


@pytest.mark.ui
def test_compact_media_card_tracks_session_without_persisting_path(qtbot, tmp_path):
    _path, sessions, _source, _directive = prepared_image(tmp_path)
    card = MediaCompactCard()
    qtbot.addWidget(card)

    card.set_snapshot(sessions.current())

    assert not card.isHidden()
    assert card.title.text() == "photo.png"
    assert str(tmp_path) not in card.title.text()
    sessions.close()
    card.set_snapshot(sessions.current())
    assert card.isHidden()


@pytest.mark.ui
@pytest.mark.parametrize(
    ("kind", "expected_page"),
    (
        (MediaKind.AUDIO, "audio_page"),
        (MediaKind.VIDEO, "video_page"),
    ),
)
def test_audio_and_video_load_into_their_in_app_viewers(
    qtbot,
    tmp_path,
    kind,
    expected_page,
):
    _path, sessions, _source, snapshot, directive = prepared_av(tmp_path, kind)
    backend = FakeMediaBackend()
    viewer = MediaViewer(sessions, playback_backend=backend)
    qtbot.addWidget(viewer)

    viewer.apply_directive(directive)

    assert viewer.stack.currentWidget() is getattr(viewer, expected_page)
    assert not viewer.av_controls.isHidden()
    assert viewer.av_fullscreen_button.isHidden() is (kind == MediaKind.AUDIO)
    assert backend.calls[0] == ("load", kind, False, 2.0)
    assert sessions.current().presentation_id == snapshot.presentation_id
    assert sessions.current().status == MediaSessionStatus.READY
    assert sessions.current().duration_seconds == 120
    assert sessions.current().position_seconds == 2
    assert viewer.position_label.text() == "0:02 / 2:00"
    viewer.shutdown()


@pytest.mark.ui
def test_audio_viewer_applies_all_playback_directives_and_ignores_stale_controls(
    qtbot,
    tmp_path,
):
    _path, sessions, _source, snapshot, show = prepared_av(
        tmp_path,
        MediaKind.AUDIO,
    )
    backend = FakeMediaBackend()
    viewer = MediaViewer(sessions, playback_backend=backend)
    qtbot.addWidget(viewer)
    viewer.apply_directive(show)
    presentation_id = snapshot.presentation_id

    def apply(action, **values):
        viewer.apply_directive(
            MediaDirective(
                presentation_id=presentation_id,
                action=action,
                **values,
            )
        )

    apply(MediaAction.PLAY)
    apply(MediaAction.PAUSE)
    apply(MediaAction.SEEK_RELATIVE, delta_seconds=30)
    apply(MediaAction.SEEK_ABSOLUTE, position_seconds=5)
    apply(MediaAction.SET_VOLUME, volume=0.5)
    apply(MediaAction.SET_RATE, playback_rate=1.5)
    apply(MediaAction.TOGGLE_MUTE)
    apply(MediaAction.STOP)

    assert ("play",) in backend.calls
    assert ("pause",) in backend.calls
    assert ("seek", 32.0) in backend.calls
    assert ("seek", 5.0) in backend.calls
    assert ("volume", 0.5) in backend.calls
    assert ("rate", 1.5) in backend.calls
    assert ("muted", True) in backend.calls
    assert backend.calls[-2:] == [("stop",), ("seek", 0.0)] or (backend.calls[-1] == ("stop",))
    assert sessions.current().status == MediaSessionStatus.STOPPED

    call_count = len(backend.calls)
    viewer.apply_directive(
        MediaDirective(
            presentation_id=uuid4(),
            action=MediaAction.PLAY,
        )
    )
    assert len(backend.calls) == call_count
    viewer.shutdown()


@pytest.mark.ui
def test_viewer_audio_focus_and_assistant_interruption_resume_safely(
    qtbot,
    tmp_path,
):
    _path, sessions, _source, snapshot, show = prepared_av(
        tmp_path,
        MediaKind.AUDIO,
    )
    aggregate_states: list[bool] = []
    focus = AudioFocusCoordinator(aggregate_states.append)
    backend = FakeMediaBackend()
    viewer = MediaViewer(
        sessions,
        playback_backend=backend,
        audio_focus=focus,
    )
    qtbot.addWidget(viewer)
    viewer.apply_directive(show)
    viewer.apply_directive(
        MediaDirective(
            presentation_id=snapshot.presentation_id,
            action=MediaAction.PLAY,
        )
    )

    assert focus.active_outputs == ("media_viewer",)
    assert aggregate_states == [True]

    viewer.suspend_for_assistant()
    assert sessions.current().status == MediaSessionStatus.PAUSED
    assert focus.output_active is False
    viewer.resume_after_assistant()
    assert sessions.current().status == MediaSessionStatus.PLAYING
    assert focus.output_active is True

    viewer.suspend_for_assistant()
    viewer.apply_directive(
        MediaDirective(
            presentation_id=snapshot.presentation_id,
            action=MediaAction.PAUSE,
        )
    )
    play_count = backend.calls.count(("play",))
    viewer.resume_after_assistant()
    assert backend.calls.count(("play",)) == play_count

    viewer.emergency_stop()
    assert sessions.current().status == MediaSessionStatus.STOPPED
    assert focus.output_active is False
    viewer.shutdown()


@pytest.mark.ui
def test_tts_focus_keeps_capture_suppressed_when_viewer_is_paused(
    qtbot,
    tmp_path,
):
    _path, sessions, _source, snapshot, show = prepared_av(
        tmp_path,
        MediaKind.VIDEO,
    )
    aggregate_states: list[bool] = []
    focus = AudioFocusCoordinator(aggregate_states.append)
    backend = FakeMediaBackend()
    viewer = MediaViewer(
        sessions,
        playback_backend=backend,
        audio_focus=focus,
    )
    qtbot.addWidget(viewer)
    viewer.apply_directive(show)
    viewer.apply_directive(
        MediaDirective(
            presentation_id=snapshot.presentation_id,
            action=MediaAction.PLAY,
        )
    )
    focus.set_output_active("tts", True)
    viewer.suspend_for_assistant()

    assert focus.active_outputs == ("tts",)
    assert aggregate_states == [True]

    focus.set_output_active("tts", False)
    assert aggregate_states == [True, False]
    viewer.shutdown()


@pytest.mark.ui
def test_compact_media_card_exposes_playback_only_for_audio_and_video(
    qtbot,
    tmp_path,
):
    card = MediaCompactCard()
    qtbot.addWidget(card)
    _path, sessions, _source, _snapshot, _directive = prepared_av(
        tmp_path,
        MediaKind.VIDEO,
    )

    card.set_snapshot(sessions.current())
    assert not card.play_pause_button.isHidden()
    sessions.update(
        sessions.current().presentation_id,
        status=MediaSessionStatus.PLAYING,
    )
    card.set_snapshot(sessions.current())
    assert card.play_pause_button.text() == "Pause"

    _path, image_sessions, _source, _directive = prepared_image(tmp_path)
    card.set_snapshot(image_sessions.current())
    assert card.play_pause_button.isHidden()


@pytest.mark.ui
def test_qt_media_backend_reports_changed_source_as_safe_error(qtbot, tmp_path):
    path, _sessions, source, _snapshot, _directive = prepared_av(
        tmp_path,
        MediaKind.AUDIO,
    )
    backend = QtMediaPlaybackBackend()
    qtbot.addWidget(backend.video_widget)
    path.write_bytes(path.read_bytes() + b"changed")

    with qtbot.waitSignal(backend.error_occurred, timeout=1000) as signal:
        backend.load(source, autoplay=False, start_seconds=None)

    assert signal.args == [
        "media_validation_failed",
        "The media file changed or is no longer available.",
    ]
    assert backend.supported_formats()
    backend.shutdown()


@pytest.mark.ui
def test_desktop_response_opens_in_app_image_viewer(
    qtbot,
    settings,
    tmp_path,
    monkeypatch,
):
    from PySide6.QtWidgets import QApplication, QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    captured_windows = []
    existing_windows = {id(widget) for widget in QApplication.topLevelWidgets()}

    def capture_window(self):
        captured_windows.extend(
            widget
            for widget in QApplication.topLevelWidgets()
            if widget.objectName() == "localAssistantWindow" and id(widget) not in existing_windows
        )
        return 0

    monkeypatch.setattr(QApplication, "exec", capture_window)
    container = build_container(settings)
    SettingsStore(container.database).set("first_run_complete", True)
    path = tmp_path / "photo.png"
    path.write_bytes(_PNG)
    source = MediaProbe().inspect(path)
    snapshot = container.media_sessions.prepare(source)
    directive = MediaDirective(
        presentation_id=snapshot.presentation_id,
        action=MediaAction.SHOW,
        source=source,
    )

    assert run_desktop(container) == 0
    window = captured_windows[-1]
    qtbot.addWidget(window)
    qtbot.addWidget(window.result_window)
    assert window._window_mode == "compact"

    window._display_response(
        Response(
            display_text="Showing photo.png.",
            response_type=ResponseType.SKILL,
            media_directive=directive,
        )
    )
    qtbot.waitUntil(
        lambda: container.media_sessions.current().status == MediaSessionStatus.READY,
        timeout=3000,
    )
    qtbot.waitUntil(window.result_window.isVisible, timeout=1000)

    assert window.media_viewer.stack.currentWidget() is window.media_viewer.image_page
    assert window._window_mode == "compact"
    assert window.result_window.stack.currentWidget() is window.result_window.media_viewer
    assert not window.compact_panel.media_card.isHidden()

    window.result_window.setGeometry(50, 60, 800, 600)
    window.shutdown()
    persisted = DesktopWindowState.model_validate(
        SettingsStore(container.database).get("desktop_window_state", {})
    )
    assert persisted.result_geometry is not None
    assert (persisted.result_geometry.width, persisted.result_geometry.height) == (
        800,
        600,
    )
    window.hide()
