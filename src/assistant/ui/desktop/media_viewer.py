from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import (
    QImage,
    QImageReader,
    QKeyEvent,
    QPainter,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from assistant.audio.focus import AudioFocusCoordinator
from assistant.contracts import MediaAction, MediaDirective, MediaKind, MediaSource
from assistant.presentation.media_probe import MediaProbeError, verify_media_source
from assistant.presentation.session import (
    MediaSessionRegistry,
    MediaSessionSnapshot,
    MediaSessionStatus,
)
from assistant.ui.desktop.media_backend import (
    MediaPlaybackBackend,
    QtMediaPlaybackBackend,
)


class ImageCanvas(QGraphicsView):
    """Zoomable and pannable image surface with bounded transforms."""

    MIN_SCALE = 0.05
    MAX_SCALE = 20.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mediaImageCanvas")
        self.setAccessibleName("Image viewer")
        self.setScene(QGraphicsScene(self))
        self._item = QGraphicsPixmapItem()
        self.scene().addItem(self._item)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self._scale_value = 1.0
        self._fit_mode = True

    @property
    def scale_value(self) -> float:
        return self._scale_value

    @property
    def has_image(self) -> bool:
        return not self._item.pixmap().isNull()

    def set_image(self, image: QImage) -> None:
        self._item.setPixmap(QPixmap.fromImage(image))
        self._item.setRotation(0)
        self.scene().setSceneRect(self._item.sceneBoundingRect())
        self.fit_image()

    def clear_image(self) -> None:
        self._item.setPixmap(QPixmap())
        self._item.setRotation(0)
        self.resetTransform()
        self._scale_value = 1.0
        self._fit_mode = True

    def fit_image(self) -> None:
        if not self.has_image:
            return
        self.resetTransform()
        self.fitInView(
            self._item.sceneBoundingRect(),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        self._scale_value = abs(self.transform().m11())
        self._fit_mode = True

    def actual_size(self) -> None:
        if not self.has_image:
            return
        self.resetTransform()
        self._scale_value = 1.0
        self._fit_mode = False
        self.centerOn(self._item)

    def zoom_by(self, factor: float) -> None:
        if not self.has_image or factor <= 0:
            return
        target = self._scale_value * factor
        bounded = min(self.MAX_SCALE, max(self.MIN_SCALE, target))
        effective = bounded / max(self._scale_value, 0.0001)
        self.scale(effective, effective)
        self._scale_value = bounded
        self._fit_mode = False

    def rotate_by(self, degrees: int) -> None:
        if not self.has_image:
            return
        self._item.setRotation((self._item.rotation() + degrees) % 360)
        self.scene().setSceneRect(self._item.sceneBoundingRect())
        if self._fit_mode:
            self.fit_image()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.angleDelta().y():
            self.zoom_by(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
            event.accept()
            return
        super().wheelEvent(event)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if self._fit_mode:
            self.fit_image()


class _ImageLoadWorker(QThread):
    loaded = Signal(object, object)
    failed = Signal(object, str, str)

    def __init__(
        self,
        directive_id: UUID,
        source: MediaSource,
        *,
        max_image_pixels: int,
    ) -> None:
        super().__init__()
        self.directive_id = directive_id
        self.source = source
        self.max_image_pixels = max_image_pixels

    def run(self) -> None:
        try:
            path = verify_media_source(self.source)
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and size.width() * size.height() > self.max_image_pixels:
                raise MediaProbeError("The image dimensions exceed the configured safety limit.")
            image = reader.read()
            if image.isNull():
                message = reader.errorString() or "The image decoder could not read this file."
                raise MediaProbeError(message)
            if image.width() * image.height() > self.max_image_pixels:
                raise MediaProbeError(
                    "The decoded image dimensions exceed the configured safety limit."
                )
            self.loaded.emit(self.directive_id, image)
        except MediaProbeError as error:
            self.failed.emit(self.directive_id, "media_validation_failed", str(error))
        except Exception:
            self.failed.emit(
                self.directive_id,
                "image_decode_failed",
                "The image could not be decoded.",
            )


class MediaViewer(QFrame):
    session_changed = Signal(object)
    fullscreen_requested = Signal(bool)
    closed = Signal()

    def __init__(
        self,
        sessions: MediaSessionRegistry,
        parent: QWidget | None = None,
        *,
        max_image_pixels: int = 100_000_000,
        playback_backend: MediaPlaybackBackend | None = None,
        audio_focus: AudioFocusCoordinator | None = None,
    ) -> None:
        super().__init__(parent)
        self.sessions = sessions
        self.max_image_pixels = max_image_pixels
        self._active_directive_id: UUID | None = None
        self._active_presentation_id: UUID | None = None
        self._workers: set[_ImageLoadWorker] = set()
        self._fullscreen = False
        self._source_kind: MediaKind | None = None
        self._duration_seconds = 0.0
        self._position_seconds = 0.0
        self._seeking = False
        self._resume_after_assistant: UUID | None = None
        self.audio_focus = audio_focus
        self.playback_backend = playback_backend or QtMediaPlaybackBackend(self)
        self.playback_backend.state_changed.connect(self._playback_state_changed)
        self.playback_backend.position_changed.connect(self._position_changed)
        self.playback_backend.duration_changed.connect(self._duration_changed)
        self.playback_backend.volume_changed.connect(self._volume_changed)
        self.playback_backend.error_occurred.connect(self._playback_error)
        self.setObjectName("mediaViewer")
        self.setAccessibleName("In-app media viewer")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        self.title = QLabel("Media viewer")
        self.title.setObjectName("mediaViewerTitle")
        self.title.setAccessibleName("Current media title")
        self.status = QLabel("READY")
        self.status.setObjectName("mediaViewerStatus")
        self.status.setAccessibleName("Media viewer status")
        self.close_button = QPushButton("Close")
        self.close_button.setAccessibleName("Close media viewer")
        self.close_button.clicked.connect(self.clear)
        header.addWidget(self.title, 1)
        header.addWidget(self.status)
        header.addWidget(self.close_button)
        layout.addLayout(header)

        self.stack = QStackedWidget()
        self.stack.setObjectName("mediaViewerStack")
        self.empty_page = self._message_page(
            "No media is open.",
            "Ask the assistant to show an approved image, audio file, or video.",
        )
        self.loading_page = self._loading_page()
        self.image_page = QWidget()
        image_layout = QVBoxLayout(self.image_page)
        image_layout.setContentsMargins(0, 0, 0, 0)
        self.image_canvas = ImageCanvas()
        image_layout.addWidget(self.image_canvas, 1)
        self.audio_page = self._audio_page()
        self.video_page = QWidget()
        video_layout = QVBoxLayout(self.video_page)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.addWidget(self.playback_backend.video_widget, 1)
        self.error_page = self._message_page(
            "Media could not be displayed.",
            "Try another supported file.",
        )
        for page in (
            self.empty_page,
            self.loading_page,
            self.image_page,
            self.audio_page,
            self.video_page,
            self.error_page,
        ):
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)

        self.image_controls = QWidget()
        controls = QHBoxLayout(self.image_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        self.fit_button = self._button("Fit", "Fit image to viewer", self.image_canvas.fit_image)
        self.actual_button = self._button(
            "100%", "Show image at actual size", self.image_canvas.actual_size
        )
        self.zoom_out_button = self._button(
            "−", "Zoom out", lambda: self.image_canvas.zoom_by(1 / 1.2)
        )
        self.zoom_in_button = self._button("+", "Zoom in", lambda: self.image_canvas.zoom_by(1.2))
        self.rotate_left_button = self._button(
            "Rotate left", "Rotate image left", lambda: self.image_canvas.rotate_by(-90)
        )
        self.rotate_right_button = self._button(
            "Rotate right", "Rotate image right", lambda: self.image_canvas.rotate_by(90)
        )
        self.fullscreen_button = self._button(
            "Fullscreen", "Toggle media fullscreen", self.toggle_fullscreen
        )
        for button in (
            self.fit_button,
            self.actual_button,
            self.zoom_out_button,
            self.zoom_in_button,
            self.rotate_left_button,
            self.rotate_right_button,
        ):
            controls.addWidget(button)
        controls.addStretch()
        controls.addWidget(self.fullscreen_button)
        layout.addWidget(self.image_controls)
        self.av_controls = self._av_controls()
        layout.addWidget(self.av_controls)

        self.stack.setCurrentWidget(self.empty_page)
        self.image_controls.hide()
        self.av_controls.hide()
        self.hide()

    @staticmethod
    def supported_image_formats() -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    bytes(value).decode("ascii", errors="ignore").casefold()
                    for value in QImageReader.supportedImageFormats()
                }
            )
        )

    def _message_page(self, title: str, detail: str) -> QWidget:
        page = QWidget()
        page.setObjectName("mediaMessagePage")
        layout = QVBoxLayout(page)
        layout.addStretch()
        heading = QLabel(title)
        heading.setObjectName("mediaMessageTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.setWordWrap(True)
        body = QLabel(detail)
        body.setObjectName("mediaMessageDetail")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addStretch()
        page.heading = heading  # type: ignore[attr-defined]
        page.detail = body  # type: ignore[attr-defined]
        return page

    def _loading_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch()
        label = QLabel("Loading media…")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setMaximumWidth(280)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(progress)
        row.addStretch()
        layout.addWidget(label)
        layout.addLayout(row)
        layout.addStretch()
        return page

    def _audio_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch()
        icon = QLabel("AUDIO")
        icon.setObjectName("mediaAudioIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setAccessibleName("Audio playback")
        detail = QLabel("Playing in the Local Assistant")
        detail.setObjectName("mediaMessageDetail")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)
        layout.addWidget(detail)
        layout.addStretch()
        return page

    def _av_controls(self) -> QWidget:
        widget = QWidget()
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        timeline = QHBoxLayout()
        self.position_label = QLabel("0:00 / 0:00")
        self.position_label.setAccessibleName("Playback position and duration")
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setAccessibleName("Media playback position")
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek_slider.sliderReleased.connect(self._seek_released)
        timeline.addWidget(self.position_label)
        timeline.addWidget(self.seek_slider, 1)
        outer.addLayout(timeline)

        controls = QHBoxLayout()
        self.play_pause_button = self._button("Play", "Play or pause media", self.play_pause)
        self.stop_button = self._button("Stop", "Stop media", self.stop_playback)
        self.back_button = self._button(
            "−10s", "Go back ten seconds", lambda: self.seek_relative(-10)
        )
        self.forward_button = self._button(
            "+10s", "Skip ahead ten seconds", lambda: self.seek_relative(10)
        )
        self.mute_button = self._button("Mute", "Mute or unmute media", self.toggle_mute)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setMaximumWidth(120)
        self.volume_slider.setAccessibleName("Media volume")
        self.volume_slider.valueChanged.connect(
            lambda value: self.playback_backend.set_volume(value / 100)
        )
        self.rate_combo = QComboBox()
        self.rate_combo.setAccessibleName("Playback speed")
        for rate in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            self.rate_combo.addItem(f"{rate:g}×", rate)
        self.rate_combo.setCurrentIndex(self.rate_combo.findData(1.0))
        self.rate_combo.currentIndexChanged.connect(self._rate_changed)
        self.av_fullscreen_button = self._button(
            "Fullscreen", "Toggle video fullscreen", self.toggle_fullscreen
        )
        for control in (
            self.play_pause_button,
            self.stop_button,
            self.back_button,
            self.forward_button,
            self.mute_button,
        ):
            controls.addWidget(control)
        controls.addWidget(self.volume_slider)
        controls.addWidget(self.rate_combo)
        controls.addStretch()
        controls.addWidget(self.av_fullscreen_button)
        outer.addLayout(controls)
        return widget

    @staticmethod
    def _button(
        label: str,
        accessible_name: str,
        callback: Callable[[], None],
    ) -> QPushButton:
        button = QPushButton(label)
        button.setAccessibleName(accessible_name)
        button.clicked.connect(callback)
        return button

    def apply_directive(self, directive: MediaDirective) -> None:
        if directive.action == MediaAction.SHOW:
            self._show_source(directive)
            return
        if (
            self._active_presentation_id is None
            or directive.presentation_id != self._active_presentation_id
        ):
            return
        if directive.action == MediaAction.FULLSCREEN:
            self.set_fullscreen(True)
        elif directive.action == MediaAction.EXIT_FULLSCREEN:
            self.set_fullscreen(False)
        elif directive.action == MediaAction.CLOSE:
            self.clear()
        elif self._source_kind in {MediaKind.AUDIO, MediaKind.VIDEO}:
            self._apply_playback_directive(directive)

    def _show_source(self, directive: MediaDirective) -> None:
        source = directive.source
        if source is None:
            self._show_error(
                directive.directive_id,
                "missing_media_source",
                "The media request did not include a source.",
            )
            return
        self._active_directive_id = directive.directive_id
        self._active_presentation_id = directive.presentation_id
        self._resume_after_assistant = None
        self._source_kind = source.kind
        self._duration_seconds = 0.0
        self._position_seconds = 0.0
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setValue(0)
        self._update_time_label()
        self.title.setText(source.title)
        self.title.setToolTip(source.origin or source.location)
        self.status.setText("LOADING")
        self.image_controls.hide()
        self.av_controls.hide()
        self.stack.setCurrentWidget(self.loading_page)
        self.show()
        self._update_session(status=MediaSessionStatus.LOADING)
        if source.kind == MediaKind.IMAGE:
            self.playback_backend.clear()
            worker = _ImageLoadWorker(
                directive.directive_id,
                source,
                max_image_pixels=self.max_image_pixels,
            )
            worker.loaded.connect(self._image_loaded)
            worker.failed.connect(self._show_error)
            worker.finished.connect(lambda worker=worker: self._discard_worker(worker))
            self._workers.add(worker)
            worker.start()
        elif source.kind == MediaKind.AUDIO:
            self.stack.setCurrentWidget(self.audio_page)
            self.av_controls.show()
            self.av_fullscreen_button.hide()
            self.playback_backend.load(
                source,
                autoplay=directive.autoplay,
                start_seconds=directive.position_seconds,
            )
        else:
            self.stack.setCurrentWidget(self.video_page)
            self.av_controls.show()
            self.av_fullscreen_button.show()
            self.playback_backend.load(
                source,
                autoplay=directive.autoplay,
                start_seconds=directive.position_seconds,
            )

    def _image_loaded(self, directive_id: UUID, image: QImage) -> None:
        if directive_id != self._active_directive_id:
            return
        self.image_canvas.set_image(image)
        self.stack.setCurrentWidget(self.image_page)
        self.image_controls.show()
        self.status.setText("IMAGE")
        self._update_session(status=MediaSessionStatus.READY)
        self.image_canvas.setFocus()

    def _apply_playback_directive(self, directive: MediaDirective) -> None:
        self._resume_after_assistant = None
        if directive.action == MediaAction.PLAY:
            self.playback_backend.play()
        elif directive.action == MediaAction.PAUSE:
            self.playback_backend.pause()
        elif directive.action == MediaAction.TOGGLE_PLAYBACK:
            self.play_pause()
        elif directive.action == MediaAction.STOP:
            self.stop_playback()
        elif directive.action == MediaAction.SEEK_RELATIVE:
            assert directive.delta_seconds is not None
            self.seek_relative(directive.delta_seconds)
        elif directive.action == MediaAction.SEEK_ABSOLUTE:
            assert directive.position_seconds is not None
            self.playback_backend.seek(directive.position_seconds)
        elif directive.action == MediaAction.SET_VOLUME:
            assert directive.volume is not None
            self.playback_backend.set_volume(directive.volume)
        elif directive.action == MediaAction.SET_RATE:
            assert directive.playback_rate is not None
            self.playback_backend.set_rate(directive.playback_rate)
        elif directive.action == MediaAction.TOGGLE_MUTE:
            self.toggle_mute()

    def play_pause(self) -> None:
        if self._source_kind not in {MediaKind.AUDIO, MediaKind.VIDEO}:
            return
        self._resume_after_assistant = None
        snapshot = self.sessions.current()
        if (
            snapshot is not None
            and snapshot.presentation_id == self._active_presentation_id
            and snapshot.status == MediaSessionStatus.PLAYING
        ):
            self.playback_backend.pause()
        else:
            self.playback_backend.play()

    def stop_playback(self) -> None:
        if self._source_kind not in {MediaKind.AUDIO, MediaKind.VIDEO}:
            return
        self._resume_after_assistant = None
        self.playback_backend.stop()

    def seek_relative(self, delta_seconds: float) -> None:
        if self._source_kind not in {MediaKind.AUDIO, MediaKind.VIDEO}:
            return
        self.playback_backend.seek(self._position_seconds + delta_seconds)

    def toggle_mute(self) -> None:
        if self._source_kind not in {MediaKind.AUDIO, MediaKind.VIDEO}:
            return
        snapshot = self.sessions.current()
        muted = (
            snapshot.muted
            if snapshot is not None and snapshot.presentation_id == self._active_presentation_id
            else False
        )
        self.playback_backend.set_muted(not muted)

    def _seek_released(self) -> None:
        self._seeking = False
        self.playback_backend.seek(self.seek_slider.value() / 1000)

    def _rate_changed(self, _index: int) -> None:
        rate = self.rate_combo.currentData()
        if rate is not None:
            self.playback_backend.set_rate(float(rate))

    def _playback_state_changed(self, status: MediaSessionStatus) -> None:
        if self._source_kind not in {MediaKind.AUDIO, MediaKind.VIDEO}:
            return
        if self.audio_focus is not None:
            self.audio_focus.set_output_active(
                "media_viewer",
                status == MediaSessionStatus.PLAYING,
            )
        self.status.setText(status.value.upper())
        self.play_pause_button.setText("Pause" if status == MediaSessionStatus.PLAYING else "Play")
        self._update_session(status=status)

    def _position_changed(self, seconds: float) -> None:
        self._position_seconds = max(0.0, seconds)
        if not self._seeking:
            self.seek_slider.setValue(round(self._position_seconds * 1000))
        self._update_time_label()
        self._update_session(position_seconds=self._position_seconds)

    def _duration_changed(self, seconds: float) -> None:
        self._duration_seconds = max(0.0, seconds)
        self.seek_slider.setRange(0, round(self._duration_seconds * 1000))
        self._update_time_label()
        self._update_session(duration_seconds=self._duration_seconds)

    def _volume_changed(self, volume: float, muted: bool) -> None:
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(round(volume * 100))
        self.volume_slider.blockSignals(False)
        self.mute_button.setText("Unmute" if muted else "Mute")
        self._update_session(volume=volume, muted=muted)

    def _playback_error(self, code: str, message: str) -> None:
        if self.audio_focus is not None:
            self.audio_focus.set_output_active("media_viewer", False)
        directive_id = self._active_directive_id
        if directive_id is not None:
            self._show_error(directive_id, code, message)

    def _update_time_label(self) -> None:
        self.position_label.setText(
            f"{_format_time(self._position_seconds)} / {_format_time(self._duration_seconds)}"
        )

    def _show_error(self, directive_id: UUID, code: str, message: str) -> None:
        if directive_id != self._active_directive_id:
            return
        self.error_page.heading.setText("Media could not be displayed.")  # type: ignore[attr-defined]
        self.error_page.detail.setText(message)  # type: ignore[attr-defined]
        self.stack.setCurrentWidget(self.error_page)
        self.image_controls.hide()
        self.av_controls.hide()
        self.status.setText("ERROR")
        self._update_session(
            status=MediaSessionStatus.FAILED,
            error_code=code,
        )

    def _update_session(self, **changes: Any) -> None:
        presentation_id = self._active_presentation_id
        if presentation_id is None:
            return
        try:
            snapshot = self.sessions.update(presentation_id, **changes)
        except ValueError:
            return
        self.session_changed.emit(snapshot)

    def toggle_fullscreen(self) -> None:
        self.set_fullscreen(not self._fullscreen)

    def suspend_for_assistant(self) -> None:
        if self._source_kind not in {MediaKind.AUDIO, MediaKind.VIDEO}:
            return
        snapshot = self.sessions.current()
        if (
            snapshot is None
            or snapshot.presentation_id != self._active_presentation_id
            or snapshot.status != MediaSessionStatus.PLAYING
        ):
            return
        self._resume_after_assistant = snapshot.presentation_id
        self.playback_backend.pause()

    def resume_after_assistant(self) -> None:
        presentation_id = self._resume_after_assistant
        self._resume_after_assistant = None
        snapshot = self.sessions.current()
        if (
            presentation_id is None
            or snapshot is None
            or snapshot.presentation_id != presentation_id
            or snapshot.status != MediaSessionStatus.PAUSED
        ):
            return
        self.playback_backend.play()

    def emergency_stop(self) -> None:
        self._resume_after_assistant = None
        if self._source_kind in {MediaKind.AUDIO, MediaKind.VIDEO}:
            self.playback_backend.stop()

    def set_fullscreen(self, enabled: bool) -> None:
        if self._fullscreen == enabled:
            return
        self._fullscreen = enabled
        self.fullscreen_button.setText("Exit fullscreen" if enabled else "Fullscreen")
        self.av_fullscreen_button.setText("Exit fullscreen" if enabled else "Fullscreen")
        self.fullscreen_requested.emit(enabled)
        self._update_session(fullscreen=enabled)

    def clear(self) -> None:
        presentation_id = self._active_presentation_id
        if self._fullscreen:
            self.set_fullscreen(False)
        self._active_directive_id = None
        self._active_presentation_id = None
        self._resume_after_assistant = None
        self._source_kind = None
        self._duration_seconds = 0
        self._position_seconds = 0
        self.playback_backend.clear()
        if self.audio_focus is not None:
            self.audio_focus.set_output_active("media_viewer", False)
        self.image_canvas.clear_image()
        self.image_controls.hide()
        self.av_controls.hide()
        self.stack.setCurrentWidget(self.empty_page)
        self.title.setText("Media viewer")
        self.status.setText("READY")
        if presentation_id is not None:
            self.sessions.close(presentation_id)
        snapshot = self.sessions.current()
        if snapshot is not None:
            self.session_changed.emit(snapshot)
        self.hide()
        self.closed.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self._fullscreen:
            self.set_fullscreen(False)
            event.accept()
            return
        if (
            self._source_kind in {MediaKind.AUDIO, MediaKind.VIDEO}
            and event.key() == Qt.Key.Key_Space
        ):
            self.play_pause()
            event.accept()
            return
        if self._source_kind in {MediaKind.AUDIO, MediaKind.VIDEO} and event.key() in {
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
        }:
            self.seek_relative(-10 if event.key() == Qt.Key.Key_Left else 10)
            event.accept()
            return
        if self._source_kind in {MediaKind.AUDIO, MediaKind.VIDEO} and event.key() == Qt.Key.Key_M:
            self.toggle_mute()
            event.accept()
            return
        if event.key() == Qt.Key.Key_F:
            self.toggle_fullscreen()
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Plus, Qt.Key.Key_Equal}:
            self.image_canvas.zoom_by(1.2)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Minus:
            self.image_canvas.zoom_by(1 / 1.2)
            event.accept()
            return
        super().keyPressEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(720, 420)

    def _discard_worker(self, worker: _ImageLoadWorker) -> None:
        self._workers.discard(worker)
        worker.deleteLater()

    def shutdown(self) -> None:
        for worker in tuple(self._workers):
            worker.requestInterruption()
            worker.wait(1000)
            self._discard_worker(worker)
        self.clear()
        self.playback_backend.shutdown()


class MediaCompactCard(QFrame):
    open_requested = Signal()
    close_requested = Signal()
    play_pause_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("compactMediaCard")
        self.setAccessibleName("Current in-app media")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(6)
        label = QLabel("NOW VIEWING")
        label.setObjectName("sectionLabel")
        layout.addWidget(label)
        row = QHBoxLayout()
        self.title = QLabel("")
        self.title.setObjectName("compactMediaTitle")
        self.title.setWordWrap(True)
        self.status = QLabel("")
        self.status.setObjectName("mediaViewerStatus")
        row.addWidget(self.title, 1)
        row.addWidget(self.status)
        layout.addLayout(row)
        controls = QHBoxLayout()
        self.play_pause_button = QPushButton("Play")
        self.play_pause_button.setAccessibleName("Play or pause current media")
        self.play_pause_button.clicked.connect(self.play_pause_requested.emit)
        self.open_button = QPushButton("Open viewer")
        self.open_button.setAccessibleName("Open full media viewer")
        self.open_button.clicked.connect(self.open_requested.emit)
        self.close_button = QPushButton("Close")
        self.close_button.setAccessibleName("Close current media")
        self.close_button.clicked.connect(self.close_requested.emit)
        controls.addWidget(self.play_pause_button)
        controls.addWidget(self.open_button)
        controls.addStretch()
        controls.addWidget(self.close_button)
        layout.addLayout(controls)
        self.hide()

    def set_snapshot(self, snapshot: MediaSessionSnapshot | None) -> None:
        if snapshot is None or snapshot.status == MediaSessionStatus.CLOSED:
            self.hide()
            return
        self.title.setText(snapshot.source.title)
        self.title.setToolTip(snapshot.source.origin or snapshot.source.location)
        self.status.setText(snapshot.status.value.upper())
        self.play_pause_button.setVisible(
            snapshot.source.kind in {MediaKind.AUDIO, MediaKind.VIDEO}
        )
        self.play_pause_button.setText(
            "Pause" if snapshot.status == MediaSessionStatus.PLAYING else "Play"
        )
        self.show()


def _format_time(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
