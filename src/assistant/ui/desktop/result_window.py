from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from assistant.ui.desktop.media_viewer import MediaViewer


class ResultWindow(QMainWindow):
    """Independent surface for media and longer automation results."""

    automation_requested = Signal(str)

    def __init__(self, media_viewer: MediaViewer) -> None:
        super().__init__(None)
        self.setObjectName("assistantResultWindow")
        self.setAccessibleName("Assistant results window")
        self.setWindowTitle("Local Assistant — Results")
        self.setMinimumSize(640, 440)
        self._run_id: str | None = None
        self._was_maximized = False

        self.stack = QStackedWidget()
        self.stack.setObjectName("resultWindowStack")
        self.media_viewer = media_viewer
        self.report_page = QWidget()
        report_layout = QVBoxLayout(self.report_page)
        report_layout.setContentsMargins(22, 20, 22, 20)
        report_layout.setSpacing(10)
        header = QHBoxLayout()
        self.report_title = QLabel("Result")
        self.report_title.setObjectName("resultWindowTitle")
        self.report_title.setWordWrap(True)
        self.report_status = QLabel("READY")
        self.report_status.setObjectName("responseBadge")
        header.addWidget(self.report_title, 1)
        header.addWidget(self.report_status)
        self.report_metadata = QLabel("")
        self.report_metadata.setObjectName("resultWindowMetadata")
        self.report_metadata.setWordWrap(True)
        self.report_body = QTextEdit()
        self.report_body.setObjectName("resultWindowBody")
        self.report_body.setAccessibleName("Automation result")
        self.report_body.setReadOnly(True)
        controls = QHBoxLayout()
        self.automation_button = QPushButton("View automation")
        self.automation_button.clicked.connect(self._open_automation)
        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        controls.addWidget(self.automation_button)
        controls.addStretch()
        controls.addWidget(close)
        report_layout.addLayout(header)
        report_layout.addWidget(self.report_metadata)
        report_layout.addWidget(self.report_body, 1)
        report_layout.addLayout(controls)

        self.stack.addWidget(self.report_page)
        self.stack.addWidget(self.media_viewer)
        self.setCentralWidget(self.stack)
        self.media_viewer.fullscreen_requested.connect(self.set_media_fullscreen)
        self.hide()

    def show_media(self) -> None:
        self._run_id = None
        self.stack.setCurrentWidget(self.media_viewer)
        self.media_viewer.show()
        self.setWindowTitle("Local Assistant — Media")
        self.show()
        self.raise_()
        self.activateWindow()
        self.media_viewer.setFocus()

    def show_report(
        self,
        *,
        title: str,
        body: str,
        metadata: str = "",
        status: str = "Completed",
        run_id: str | None = None,
    ) -> None:
        self._run_id = run_id
        self.report_title.setText(title)
        self.report_body.setPlainText(body)
        self.report_metadata.setText(metadata)
        self.report_metadata.setVisible(bool(metadata))
        self.report_status.setText(status.upper())
        self.automation_button.setVisible(run_id is not None)
        self.stack.setCurrentWidget(self.report_page)
        self.setWindowTitle(f"Local Assistant — {title}")
        self.show()
        self.raise_()
        self.activateWindow()
        self.report_body.setFocus()

    def set_media_fullscreen(self, enabled: bool) -> None:
        if enabled:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        elif self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and not self.isFullScreen():
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def _open_automation(self) -> None:
        if self._run_id is not None:
            self.automation_requested.emit(self._run_id)
