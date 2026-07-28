from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaFormat, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QWidget

from assistant.contracts import MediaKind, MediaSource
from assistant.presentation.media_probe import verify_media_source
from assistant.presentation.session import MediaSessionStatus


class MediaPlaybackBackend(QObject):
    """Testable signal boundary for audio/video playback implementations."""

    state_changed = Signal(object)
    position_changed = Signal(float)
    duration_changed = Signal(float)
    volume_changed = Signal(float, bool)
    error_occurred = Signal(str, str)

    video_widget: QWidget

    def load(
        self,
        source: MediaSource,
        *,
        autoplay: bool,
        start_seconds: float | None,
    ) -> None:
        raise NotImplementedError

    def play(self) -> None:
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def seek(self, seconds: float) -> None:
        raise NotImplementedError

    def set_volume(self, volume: float) -> None:
        raise NotImplementedError

    def set_muted(self, muted: bool) -> None:
        raise NotImplementedError

    def set_rate(self, rate: float) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError


class QtMediaPlaybackBackend(MediaPlaybackBackend):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.video_widget = QVideoWidget()
        self.video_widget.setObjectName("mediaVideoSurface")
        self.video_widget.setAccessibleName("Video playback surface")
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self._loaded = False
        self._autoplay = False
        self._start_seconds: float | None = None
        self._pending_start_applied = False
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.positionChanged.connect(
            lambda milliseconds: self.position_changed.emit(milliseconds / 1000)
        )
        self.player.durationChanged.connect(
            lambda milliseconds: self.duration_changed.emit(milliseconds / 1000)
        )
        self.player.errorOccurred.connect(self._player_error)
        self.audio_output.volumeChanged.connect(
            lambda volume: self.volume_changed.emit(
                float(volume),
                self.audio_output.isMuted(),
            )
        )
        self.audio_output.mutedChanged.connect(
            lambda muted: self.volume_changed.emit(
                float(self.audio_output.volume()),
                bool(muted),
            )
        )

    @staticmethod
    def supported_formats() -> tuple[str, ...]:
        media_format = QMediaFormat()
        return tuple(
            sorted(
                {
                    value.name.casefold()
                    for value in media_format.supportedFileFormats(
                        QMediaFormat.ConversionMode.Decode
                    )
                }
            )
        )

    def load(
        self,
        source: MediaSource,
        *,
        autoplay: bool,
        start_seconds: float | None,
    ) -> None:
        if source.kind not in {MediaKind.AUDIO, MediaKind.VIDEO}:
            raise ValueError("Qt media playback requires audio or video")
        self.clear()
        try:
            path = verify_media_source(source)
        except Exception:
            self.error_occurred.emit(
                "media_validation_failed",
                "The media file changed or is no longer available.",
            )
            return
        self._autoplay = autoplay
        self._start_seconds = start_seconds
        self._pending_start_applied = False
        self.state_changed.emit(MediaSessionStatus.LOADING)
        self.player.setSource(QUrl.fromLocalFile(str(path)))

    def play(self) -> None:
        self.player.play()

    def pause(self) -> None:
        self.player.pause()

    def stop(self) -> None:
        self.player.stop()

    def seek(self, seconds: float) -> None:
        bounded = max(0.0, float(seconds))
        duration = self.player.duration() / 1000
        if duration > 0:
            bounded = min(bounded, duration)
        self.player.setPosition(round(bounded * 1000))

    def set_volume(self, volume: float) -> None:
        self.audio_output.setVolume(min(1.0, max(0.0, float(volume))))

    def set_muted(self, muted: bool) -> None:
        self.audio_output.setMuted(bool(muted))

    def set_rate(self, rate: float) -> None:
        self.player.setPlaybackRate(min(4.0, max(0.25, float(rate))))

    def clear(self) -> None:
        self._loaded = False
        self._autoplay = False
        self._start_seconds = None
        self._pending_start_applied = False
        self.player.stop()
        self.player.setSource(QUrl())

    def shutdown(self) -> None:
        self.clear()

    def _media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status in {
            QMediaPlayer.MediaStatus.LoadingMedia,
            QMediaPlayer.MediaStatus.StalledMedia,
        }:
            self.state_changed.emit(MediaSessionStatus.LOADING)
            return
        if status in {
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        }:
            self._loaded = True
            self.state_changed.emit(MediaSessionStatus.READY)
            if not self._pending_start_applied and self._start_seconds is not None:
                self.seek(self._start_seconds)
                self._pending_start_applied = True
            if self._autoplay:
                self._autoplay = False
                self.play()
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.state_changed.emit(MediaSessionStatus.STOPPED)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.error_occurred.emit(
                "unsupported_codec",
                "This audio or video format is not supported by the installed decoder.",
            )

    def _playback_state_changed(
        self,
        state: QMediaPlayer.PlaybackState,
    ) -> None:
        if not self._loaded:
            return
        mapped = {
            QMediaPlayer.PlaybackState.PlayingState: MediaSessionStatus.PLAYING,
            QMediaPlayer.PlaybackState.PausedState: MediaSessionStatus.PAUSED,
            QMediaPlayer.PlaybackState.StoppedState: MediaSessionStatus.STOPPED,
        }[state]
        self.state_changed.emit(mapped)

    def _player_error(
        self,
        error: QMediaPlayer.Error,
        _error_string: str,
    ) -> None:
        code = {
            QMediaPlayer.Error.ResourceError: "media_resource_error",
            QMediaPlayer.Error.FormatError: "unsupported_codec",
            QMediaPlayer.Error.NetworkError: "media_network_error",
            QMediaPlayer.Error.AccessDeniedError: "media_access_denied",
        }.get(error, "media_playback_error")
        message = {
            "media_resource_error": "The media resource could not be read.",
            "unsupported_codec": (
                "This audio or video format is not supported by the installed decoder."
            ),
            "media_network_error": "The media resource could not be loaded.",
            "media_access_denied": "Access to the media resource was denied.",
        }.get(code, "Audio or video playback failed.")
        self.error_occurred.emit(code, message)


def media_file_url(path: str | Path) -> QUrl:
    """Small public helper used by smoke tests and diagnostics."""

    return QUrl.fromLocalFile(str(Path(path).resolve()))
