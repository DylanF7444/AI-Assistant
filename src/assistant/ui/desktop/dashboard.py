from __future__ import annotations

import time
from datetime import datetime
from uuid import UUID

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from assistant.calendar.contracts import CalendarSnapshot
from assistant.dashboard.contracts import (
    DashboardCard,
    MetricDefinition,
    MetricPoint,
    MetricPresentation,
    MetricSnapshot,
    MetricState,
)
from assistant.ui.desktop.calendar_view import CalendarLayoutWidget
from assistant.ui.desktop.controller import DesktopController


class DashboardSparkline(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: tuple[MetricPoint, ...] = ()
        self.setObjectName("dashboardSparkline")
        self.setMinimumHeight(44)

    def set_points(self, points: tuple[MetricPoint, ...]) -> None:
        self._points = points
        self.setVisible(len(points) >= 2)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if len(self._points) < 2:
            return
        values = [point.value for point in self._points]
        low = min(values)
        high = max(values)
        spread = high - low
        bounds = self.rect().adjusted(2, 3, -2, -3)
        path = QPainterPath()
        for index, value in enumerate(values):
            x = bounds.left() + (bounds.width() * index / (len(values) - 1))
            normalized = 0.5 if spread == 0 else (value - low) / spread
            y = bounds.bottom() - bounds.height() * normalized
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#5a8cff"), 2))
        painter.drawPath(path)


class DashboardMetricCard(QFrame):
    permission_requested = Signal(object)

    def __init__(
        self,
        card: DashboardCard,
        definition: MetricDefinition | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.card = card
        self.definition = definition
        self.setObjectName("dashboardMetricCard")
        self.setProperty("state", "loading")
        self.setMinimumSize(240, 168)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(7)

        heading = QHBoxLayout()
        self.title = QLabel(
            card.title
            or (definition.display_name if definition is not None else "Unavailable metric")
        )
        self.title.setObjectName("dashboardCardTitle")
        self.state_label = QLabel("LOADING")
        self.state_label.setObjectName("dashboardCardState")
        heading.addWidget(self.title, 1)
        heading.addWidget(self.state_label)

        self.value = QLabel("—")
        self.value.setObjectName("dashboardCardValue")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.progress = QProgressBar()
        self.progress.setObjectName("dashboardMetricProgress")
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.sparkline = DashboardSparkline()
        self.sparkline.hide()
        self.summary = QLabel("Waiting for the first refresh.")
        self.summary.setObjectName("dashboardCardSummary")
        self.summary.setWordWrap(True)
        self.details = QLabel("")
        self.details.setObjectName("dashboardCardDetails")
        self.details.setWordWrap(True)
        self.permission_button = QPushButton("Grant access")
        self.permission_button.setObjectName("secondaryButton")
        self.permission_button.setAccessibleName(f"Grant access for {self.title.text()}")
        self.permission_button.clicked.connect(
            lambda: self.permission_requested.emit(self.card.card_id)
        )
        self.permission_button.hide()

        source = definition.source_label if definition is not None else card.metric_id
        self.source = QLabel(f"{source}  •  Not refreshed")
        self.source.setObjectName("dashboardCardSource")

        layout.addLayout(heading)
        layout.addWidget(self.value)
        layout.addWidget(self.progress)
        layout.addWidget(self.sparkline)
        layout.addWidget(self.summary)
        layout.addWidget(self.details)
        layout.addWidget(self.permission_button)
        layout.addStretch()
        layout.addWidget(self.source)
        self._update_accessible_name()

    def set_loading(self) -> None:
        self.permission_button.hide()
        self.state_label.setText("REFRESHING")
        self._set_state("loading")

    def apply_snapshot(self, snapshot: MetricSnapshot) -> None:
        self._set_state(snapshot.state.value)
        self.state_label.setText(snapshot.state.value.replace("_", " ").upper())
        self.value.setText(self._format_value(snapshot))
        numeric = not isinstance(snapshot.value, bool) and isinstance(snapshot.value, (int, float))
        show_progress = (
            self.card.presentation in (MetricPresentation.PERCENTAGE, MetricPresentation.PROGRESS)
            and numeric
        )
        self.progress.setVisible(show_progress)
        if show_progress:
            self.progress.setValue(round(max(0.0, min(100.0, float(snapshot.value)))))
        show_trend = self.card.presentation == MetricPresentation.TREND and len(snapshot.trend) >= 2
        self.sparkline.set_points(snapshot.trend if show_trend else ())
        self.summary.setText(snapshot.summary or "")
        self.details.setText(
            "    ".join(f"{item.label}: {item.value}" for item in snapshot.details)
        )
        self.permission_button.setVisible(snapshot.state == MetricState.PERMISSION_REQUIRED)
        source = self.definition.source_label if self.definition is not None else snapshot.metric_id
        refreshed = _local_time(snapshot.collected_at)
        self.source.setText(f"{source}  •  Updated {refreshed}")
        self._update_accessible_name()

    def set_refresh_error(self) -> None:
        self._set_state(MetricState.ERROR.value)
        self.state_label.setText("ERROR")
        self.summary.setText("Dashboard refresh failed. Try again.")
        self._update_accessible_name()

    def _set_state(self, state: str) -> None:
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)

    @staticmethod
    def _format_value(snapshot: MetricSnapshot) -> str:
        value = snapshot.value
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "On" if value else "Off"
        if isinstance(value, float):
            rendered = f"{value:,.1f}"
        elif isinstance(value, int):
            rendered = f"{value:,}"
        else:
            rendered = str(value)
        return f"{rendered} {snapshot.unit}".strip() if snapshot.unit else rendered

    def _update_accessible_name(self) -> None:
        self.setAccessibleName(
            f"{self.title.text()}: {self.value.text()}. "
            f"{self.state_label.text()}. {self.summary.text()}"
        )


class DashboardPage(QWidget):
    refresh_requested = Signal()
    refresh_due_requested = Signal(object)
    calendar_refresh_due_requested = Signal()
    calendar_refresh_requested = Signal()
    calendar_permission_requested = Signal()
    calendar_extension_requested = Signal(str)
    calendar_layout_changed = Signal(object)
    customize_requested = Signal()
    connections_requested = Signal()
    work_brief_requested = Signal()
    permission_requested = Signal(object)
    became_visible = Signal()

    def __init__(
        self,
        controller: DesktopController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("dashboardPage")
        self._refreshing = False
        self._active_card_ids: tuple[UUID, ...] = ()
        self._last_refreshed_at: dict[UUID, float] = {}
        self._calendar_last_refreshed_at = 0.0
        self._calendar_refreshing = False
        self._column_count = 0
        self._cards: dict[UUID, DashboardMetricCard] = {}
        self._ordered_cards: list[tuple[DashboardCard, DashboardMetricCard]] = []

        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(28, 24, 28, 24)
        page_layout.setSpacing(16)

        heading = QHBoxLayout()
        heading_text = QVBoxLayout()
        eyebrow = QLabel("LOCAL OVERVIEW")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Dashboard")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "A privacy-bounded view of assistant performance, activity, automations, and calendar."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        heading_text.addWidget(eyebrow)
        heading_text.addWidget(title)
        heading_text.addWidget(subtitle)

        controls = QVBoxLayout()
        self.connections_button = QPushButton("Connections")
        self.connections_button.setObjectName("secondaryButton")
        self.connections_button.setAccessibleName("Manage dashboard connections")
        self.connections_button.clicked.connect(self.connections_requested)
        self.work_brief_button = QPushButton("Work brief")
        self.work_brief_button.setObjectName("primaryButton")
        self.work_brief_button.setAccessibleName("Build work brief from configured inputs")
        self.work_brief_button.clicked.connect(self.work_brief_requested)
        self.customize_button = QPushButton("Customize")
        self.customize_button.setObjectName("secondaryButton")
        self.customize_button.setAccessibleName("Customize dashboard cards")
        self.customize_button.clicked.connect(self.customize_requested)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.setAccessibleName("Refresh dashboard metrics")
        self.refresh_button.clicked.connect(self.refresh_requested)
        self.last_refresh = QLabel("Not refreshed")
        self.last_refresh.setObjectName("dashboardLastRefresh")
        self.last_refresh.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        control_buttons = QHBoxLayout()
        control_buttons.addWidget(self.work_brief_button)
        control_buttons.addWidget(self.connections_button)
        control_buttons.addWidget(self.customize_button)
        control_buttons.addWidget(self.refresh_button)
        controls.addLayout(control_buttons)
        controls.addWidget(self.last_refresh)

        heading.addLayout(heading_text, 1)
        heading.addLayout(controls)
        page_layout.addLayout(heading)

        self.calendar = CalendarLayoutWidget(controller)
        self.calendar.refresh_requested.connect(self.calendar_refresh_requested.emit)
        self.calendar.permission_requested.connect(self.calendar_permission_requested.emit)
        self.calendar.extension_requested.connect(self.calendar_extension_requested.emit)
        self.calendar.layout_changed.connect(self.calendar_layout_changed.emit)
        self.calendar.range_changed.connect(self.calendar_refresh_requested.emit)
        page_layout.addWidget(self.calendar)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("dashboardScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.card_container = QWidget()
        self.card_container.setObjectName("dashboardCardContainer")
        self.card_grid = QGridLayout(self.card_container)
        self.card_grid.setContentsMargins(0, 0, 0, 0)
        self.card_grid.setHorizontalSpacing(14)
        self.card_grid.setVerticalSpacing(14)
        self.scroll.setWidget(self.card_container)
        page_layout.addWidget(self.scroll, 1)

        self._build_cards()
        self._reflow_cards(force=True)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._request_due_refresh)

    @property
    def refreshing(self) -> bool:
        return self._refreshing

    @property
    def calendar_refreshing(self) -> bool:
        return self._calendar_refreshing

    def begin_refresh(
        self,
        card_ids: tuple[UUID, ...] | None = None,
    ) -> bool:
        if self._refreshing:
            return False
        selected = (
            tuple(self._cards)
            if card_ids is None
            else tuple(card_id for card_id in card_ids if card_id in self._cards)
        )
        if not selected:
            return False
        self._refreshing = True
        self._active_card_ids = selected
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Refreshing…")
        self.last_refresh.setText("Refreshing metrics…")
        for card_id in selected:
            self._cards[card_id].set_loading()
        return True

    def apply_snapshots(self, snapshots: dict[UUID, MetricSnapshot]) -> None:
        refreshed_at = time.monotonic()
        for card_id, snapshot in snapshots.items():
            card = self._cards.get(card_id)
            if card is not None:
                card.apply_snapshot(snapshot)
                self._last_refreshed_at[card_id] = refreshed_at
        self._finish_refresh()
        self.last_refresh.setText(f"Updated {_local_time(datetime.now().astimezone())}")

    def begin_calendar_refresh(self) -> bool:
        if self._calendar_refreshing:
            return False
        self._calendar_refreshing = True
        self.calendar.set_loading()
        return True

    def apply_calendar_snapshot(self, snapshot: CalendarSnapshot) -> None:
        self._calendar_refreshing = False
        self._calendar_last_refreshed_at = time.monotonic()
        self.calendar.set_snapshot(snapshot)

    def fail_calendar_refresh(self, message: str) -> None:
        self._calendar_refreshing = False
        self._calendar_last_refreshed_at = time.monotonic()
        self.calendar.set_refresh_error(message)

    def fail_refresh(self, _message: str) -> None:
        attempted_at = time.monotonic()
        for card_id in self._active_card_ids:
            card = self._cards.get(card_id)
            if card is not None:
                card.set_refresh_error()
                self._last_refreshed_at[card_id] = attempted_at
        self._finish_refresh()
        self.last_refresh.setText("Refresh failed")

    def reload_layout(self) -> None:
        while self.card_grid.count():
            item = self.card_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._cards.clear()
        self._ordered_cards.clear()
        self._column_count = 0
        self._build_cards()
        self._last_refreshed_at = {
            card_id: refreshed_at
            for card_id, refreshed_at in self._last_refreshed_at.items()
            if card_id in self._cards
        }
        self._reflow_cards(force=True)
        self.last_refresh.setText("Layout saved  •  Refreshing…")

    def reload_calendar_layout(self) -> None:
        self.calendar.set_layout(self.controller.calendar_layout())
        self._calendar_last_refreshed_at = 0.0

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_timer.start()
        self.became_visible.emit()

    def hideEvent(self, event) -> None:
        self._refresh_timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_cards()

    def _build_cards(self) -> None:
        definitions = {
            definition.metric_id: definition for definition in self.controller.dashboard_catalog()
        }
        layout = self.controller.dashboard_layout()
        for card in sorted(layout.cards, key=lambda item: item.position):
            if not card.enabled:
                continue
            widget = DashboardMetricCard(card, definitions.get(card.metric_id))
            widget.permission_requested.connect(self.permission_requested)
            self._cards[card.card_id] = widget
            self._ordered_cards.append((card, widget))

    def _reflow_cards(self, *, force: bool = False) -> None:
        width = max(self.width(), self.scroll.viewport().width())
        columns = 1 if width < 700 else 2 if width < 1080 else 3 if width < 1440 else 4
        if not force and columns == self._column_count:
            return
        self._column_count = columns
        while self.card_grid.count():
            self.card_grid.takeAt(0)
        for column in range(4):
            self.card_grid.setColumnStretch(column, 1 if column < columns else 0)

        row = 0
        column = 0
        for card, widget in self._ordered_cards:
            span = 2 if card.size == "wide" and columns > 1 else 1
            if column + span > columns:
                row += 1
                column = 0
            self.card_grid.addWidget(widget, row, column, 1, span)
            column += span
            if column >= columns:
                row += 1
                column = 0
        self.card_grid.setRowStretch(row + 1, 1)

    def _finish_refresh(self) -> None:
        self._refreshing = False
        self._active_card_ids = ()
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Refresh")

    def _request_due_refresh(self) -> None:
        if self._refreshing or not self.isVisible():
            return
        now = time.monotonic()
        due = tuple(
            card.card_id
            for card, _widget in self._ordered_cards
            if now - self._last_refreshed_at.get(card.card_id, 0) >= card.refresh_seconds
        )
        if due:
            self.refresh_due_requested.emit(due)
        if (
            not self._calendar_refreshing
            and now - self._calendar_last_refreshed_at >= self.controller.calendar_refresh_seconds()
        ):
            self.calendar_refresh_due_requested.emit()

    def shutdown(self) -> None:
        self._refresh_timer.stop()


def _local_time(value: datetime) -> str:
    return value.astimezone().strftime("%H:%M:%S")
