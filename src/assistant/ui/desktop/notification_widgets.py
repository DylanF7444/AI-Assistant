from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from assistant.scheduling.notifications import NotificationMessage


class NotificationBanner(QFrame):
    """Short-lived, nonblocking foreground notification."""

    open_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notificationBanner")
        self.setAccessibleName("Assistant notification")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(360)
        self.setMaximumWidth(520)
        self._notification_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 12, 12)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.title = QLabel("")
        self.title.setObjectName("notificationBannerTitle")
        self.title.setWordWrap(True)
        self.close_button = QPushButton("×")
        self.close_button.setObjectName("notificationBannerClose")
        self.close_button.setAccessibleName("Hide notification")
        self.close_button.setFixedSize(28, 28)
        self.close_button.clicked.connect(self.hide)
        header.addWidget(self.title, 1)
        header.addWidget(self.close_button)
        self.message = QLabel("")
        self.message.setObjectName("notificationBannerMessage")
        self.message.setWordWrap(True)
        actions = QHBoxLayout()
        actions.addStretch()
        self.open_button = QPushButton("Open")
        self.open_button.setObjectName("notificationBannerOpen")
        self.open_button.clicked.connect(self._open)
        actions.addWidget(self.open_button)
        layout.addLayout(header)
        layout.addWidget(self.message)
        layout.addLayout(actions)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_notification(
        self,
        notification: NotificationMessage,
        *,
        timeout_ms: int = 8_000,
    ) -> None:
        self._notification_id = str(notification.notification_id)
        self.title.setText(notification.title)
        self.message.setText(notification.message)
        self.setProperty("severity", notification.severity)
        self.style().unpolish(self)
        self.style().polish(self)
        self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(timeout_ms)

    def _open(self) -> None:
        if self._notification_id is not None:
            self.open_requested.emit(self._notification_id)
        self.hide()


class NotificationDrawer(QFrame):
    """Overlay listing the most recent durable notifications."""

    open_requested = Signal(str)
    dismiss_requested = Signal(str)
    see_all_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notificationDrawer")
        self.setAccessibleName("Notification drawer")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Notifications")
        title.setObjectName("notificationDrawerTitle")
        self.unread = QLabel("")
        self.unread.setObjectName("notificationUnreadBadge")
        close = QPushButton("×")
        close.setAccessibleName("Close notification drawer")
        close.setFixedSize(28, 28)
        close.clicked.connect(self.hide)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.unread)
        header.addWidget(close)
        layout.addLayout(header)

        self.items = QListWidget()
        self.items.setObjectName("notificationDrawerList")
        self.items.setAccessibleName("Recent notifications")
        self.items.itemDoubleClicked.connect(lambda _item: self._open_selected())
        layout.addWidget(self.items, 1)

        actions = QHBoxLayout()
        self.open_button = QPushButton("Open")
        self.open_button.clicked.connect(self._open_selected)
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.clicked.connect(self._dismiss_selected)
        see_all = QPushButton("See all")
        see_all.clicked.connect(self.see_all_requested.emit)
        actions.addWidget(self.open_button)
        actions.addWidget(self.dismiss_button)
        actions.addStretch()
        actions.addWidget(see_all)
        layout.addLayout(actions)
        self.hide()

    def refresh(
        self,
        notifications: Sequence[NotificationMessage],
        *,
        unread_count: int,
    ) -> None:
        selected_id = self.selected_id()
        self.items.clear()
        for notification in notifications:
            state = "NEW · " if notification.read_at is None else ""
            created = notification.created_at.astimezone().strftime("%b %d, %I:%M %p")
            item = QListWidgetItem(
                f"{state}{notification.title}\n{notification.message}\n{created}"
            )
            item.setData(Qt.ItemDataRole.UserRole, str(notification.notification_id))
            item.setToolTip(notification.message)
            self.items.addItem(item)
            if selected_id == str(notification.notification_id):
                self.items.setCurrentItem(item)
        self.unread.setText(str(unread_count) if unread_count else "")
        self.unread.setVisible(unread_count > 0)
        has_items = self.items.count() > 0
        self.open_button.setEnabled(has_items)
        self.dismiss_button.setEnabled(has_items)

    def selected_id(self) -> str | None:
        item = self.items.currentItem()
        if item is None and self.items.count():
            item = self.items.item(0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return str(value) if value else None

    def _open_selected(self) -> None:
        notification_id = self.selected_id()
        if notification_id:
            self.open_requested.emit(notification_id)

    def _dismiss_selected(self) -> None:
        notification_id = self.selected_id()
        if notification_id:
            self.dismiss_requested.emit(notification_id)
