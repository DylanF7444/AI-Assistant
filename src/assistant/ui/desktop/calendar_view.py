from __future__ import annotations

from datetime import date, datetime, time, timedelta
from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from assistant.calendar.contracts import (
    CalendarEvent,
    CalendarLayout,
    CalendarQuery,
    CalendarSnapshot,
    CalendarState,
    CalendarView,
)
from assistant.ui.desktop.controller import DesktopController


class CalendarLayoutWidget(QFrame):
    """Reusable calendar surface populated by provider-neutral snapshots."""

    refresh_requested = Signal()
    range_changed = Signal()
    permission_requested = Signal()
    extension_requested = Signal(str)
    layout_changed = Signal(object)

    def __init__(
        self,
        controller: DesktopController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self._layout = controller.calendar_layout()
        self._anchor = datetime.now().astimezone().date()
        self._snapshot: CalendarSnapshot | None = None
        self._loading = False
        self._hide_sensitive = controller.hide_sensitive_display()
        self.setObjectName("calendarSurface")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(280)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        heading = QHBoxLayout()
        heading_text = QVBoxLayout()
        eyebrow = QLabel("CALENDAR")
        eyebrow.setObjectName("sectionLabel")
        self.title = QLabel("Upcoming")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setObjectName("calendarTitle")
        self.subtitle = QLabel("A provider-neutral calendar surface for connected extensions.")
        self.subtitle.setTextFormat(Qt.TextFormat.PlainText)
        self.subtitle.setObjectName("calendarSubtitle")
        self.subtitle.setWordWrap(True)
        heading_text.addWidget(eyebrow)
        heading_text.addWidget(self.title)
        heading_text.addWidget(self.subtitle)
        heading.addLayout(heading_text, 1)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.previous_button = QPushButton("‹")
        self.previous_button.setObjectName("secondaryButton")
        self.previous_button.setAccessibleName("Previous calendar range")
        self.previous_button.clicked.connect(lambda: self._move(-1))
        self.today_button = QPushButton("Today")
        self.today_button.setObjectName("secondaryButton")
        self.today_button.setAccessibleName("Show today in calendar")
        self.today_button.clicked.connect(self._today)
        self.next_button = QPushButton("›")
        self.next_button.setObjectName("secondaryButton")
        self.next_button.setAccessibleName("Next calendar range")
        self.next_button.clicked.connect(lambda: self._move(1))
        self.provider_input = QComboBox()
        self.provider_input.setObjectName("calendarProviderInput")
        self.provider_input.setAccessibleName("Calendar provider")
        self._populate_provider_input()
        self.provider_input.currentIndexChanged.connect(self._provider_changed)
        self.view_input = QComboBox()
        self.view_input.setObjectName("calendarViewInput")
        for view, label in (
            (CalendarView.MONTH, "Month"),
            (CalendarView.WEEK, "Week"),
            (CalendarView.AGENDA, "Agenda"),
        ):
            self.view_input.addItem(label, view.value)
        self.view_input.setCurrentIndex(self.view_input.findData(self._layout.view.value))
        self.view_input.currentIndexChanged.connect(self._view_changed)
        self.sync_button = QPushButton("Sync")
        self.sync_button.setObjectName("secondaryButton")
        self.sync_button.setAccessibleName("Sync calendar events")
        self.sync_button.clicked.connect(self.refresh_requested)
        self.access_button = QPushButton("Grant access")
        self.access_button.setObjectName("secondaryButton")
        self.access_button.setAccessibleName("Grant calendar dashboard access")
        self.access_button.clicked.connect(self.permission_requested)
        self.access_button.hide()
        self.extension_button = QPushButton("Open Extension Hub")
        self.extension_button.setObjectName("secondaryButton")
        self.extension_button.setAccessibleName("Open calendar extension settings")
        self.extension_button.clicked.connect(self._request_extension)
        self.extension_button.hide()
        controls.addWidget(self.previous_button)
        controls.addWidget(self.today_button)
        controls.addWidget(self.next_button)
        controls.addWidget(self.provider_input)
        controls.addWidget(self.view_input)
        controls.addWidget(self.sync_button)
        controls.addWidget(self.access_button)
        controls.addWidget(self.extension_button)
        heading.addLayout(controls)
        root.addLayout(heading)

        metadata = QHBoxLayout()
        self.range_label = QLabel(self._range_label())
        self.range_label.setTextFormat(Qt.TextFormat.PlainText)
        self.range_label.setObjectName("calendarRange")
        self.provider_label = QLabel(self._provider_label())
        self.provider_label.setTextFormat(Qt.TextFormat.PlainText)
        self.provider_label.setObjectName("calendarProvider")
        self.status_label = QLabel("Not synced")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setObjectName("calendarStatus")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.weekends_input = QCheckBox("Weekends")
        self.weekends_input.setObjectName("calendarWeekendsInput")
        self.weekends_input.setAccessibleName("Show calendar weekends")
        self.weekends_input.setChecked(self._layout.show_weekends)
        self.weekends_input.toggled.connect(self._display_preferences_changed)
        self.declined_input = QCheckBox("Declined")
        self.declined_input.setObjectName("calendarDeclinedInput")
        self.declined_input.setAccessibleName("Show declined calendar events")
        self.declined_input.setChecked(self._layout.show_declined)
        self.declined_input.toggled.connect(self._display_preferences_changed)
        metadata.addWidget(self.range_label)
        metadata.addWidget(self.provider_label)
        metadata.addStretch()
        metadata.addWidget(self.weekends_input)
        metadata.addWidget(self.declined_input)
        metadata.addWidget(self.status_label)
        root.addLayout(metadata)

        self.body = QWidget()
        self.body.setObjectName("calendarBody")
        self.body_layout = QGridLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setHorizontalSpacing(6)
        self.body_layout.setVerticalSpacing(6)
        root.addWidget(self.body)
        self._render()

    @property
    def layout_config(self) -> CalendarLayout:
        return self._layout

    @property
    def anchor_date(self) -> date:
        return self._anchor

    @property
    def snapshot(self) -> CalendarSnapshot | None:
        return self._snapshot

    def set_layout(self, layout: CalendarLayout) -> None:
        self._layout = CalendarLayout.model_validate(layout)
        self._loading = False
        self._invalidate_snapshot()
        self._populate_provider_input()
        index = self.view_input.findData(self._layout.view.value)
        self.view_input.blockSignals(True)
        self.view_input.setCurrentIndex(max(0, index))
        self.view_input.blockSignals(False)
        self.weekends_input.blockSignals(True)
        self.weekends_input.setChecked(self._layout.show_weekends)
        self.weekends_input.blockSignals(False)
        self.declined_input.blockSignals(True)
        self.declined_input.setChecked(self._layout.show_declined)
        self.declined_input.blockSignals(False)
        self.provider_label.setText(self._provider_label())
        self._render()

    def _populate_provider_input(self) -> None:
        definitions = self.controller.calendar_provider_definitions()
        self.provider_input.blockSignals(True)
        self.provider_input.clear()
        for definition in definitions:
            self.provider_input.addItem(definition.display_name, definition.provider_id)
        if self.provider_input.findData(self._layout.provider_id) < 0:
            self.provider_input.addItem(self._layout.provider_id, self._layout.provider_id)
        self.provider_input.setCurrentIndex(
            max(0, self.provider_input.findData(self._layout.provider_id))
        )
        self.provider_input.blockSignals(False)

    def set_sensitive_hidden(self, hidden: bool) -> None:
        self._hide_sensitive = bool(hidden)
        self.provider_label.setText(self._provider_label())
        self._render()

    def set_loading(self) -> None:
        self._loading = True
        self.sync_button.setEnabled(False)
        self.status_label.setText("Syncing…")
        self._render()

    def set_snapshot(self, snapshot: CalendarSnapshot) -> None:
        self._loading = False
        self._snapshot = snapshot
        self.sync_button.setEnabled(True)
        self._set_state(snapshot.state)
        self.status_label.setText(self._status_text(snapshot))
        self.access_button.setVisible(snapshot.state == CalendarState.PERMISSION_REQUIRED)
        self.extension_button.setVisible(
            snapshot.state
            in {
                CalendarState.UNAVAILABLE,
                CalendarState.DISCONNECTED,
                CalendarState.REAUTHORIZATION_REQUIRED,
            }
            and self._extension_id() is not None
        )
        self._render()

    def set_refresh_error(self, _message: str = "") -> None:
        self._loading = False
        self.sync_button.setEnabled(True)
        if self._snapshot is not None and self._snapshot.events:
            self._snapshot = self._snapshot.model_copy(
                update={
                    "state": CalendarState.STALE,
                    "summary": "Calendar refresh failed. Showing the last refreshed events.",
                    "error_code": "refresh_failed",
                }
            )
        self._set_state(CalendarState.ERROR)
        self.status_label.setText("Calendar refresh failed")
        self._render()

    def current_query(self) -> CalendarQuery:
        start, end = self.visible_range()
        return self.controller.calendar_query(start=start, end=end, layout=self._layout)

    def visible_range(self) -> tuple[datetime, datetime]:
        local_zone = datetime.now().astimezone().tzinfo
        start_date, day_count = self._visible_dates()
        start = datetime.combine(start_date, time.min, tzinfo=local_zone)
        return start, start + timedelta(days=day_count)

    def _visible_dates(self) -> tuple[date, int]:
        if self._layout.view == CalendarView.AGENDA:
            return self._anchor, self._layout.horizon_days
        if self._layout.view == CalendarView.WEEK:
            offset = (self._anchor.weekday() - self._layout.first_day_of_week) % 7
            return self._anchor - timedelta(days=offset), 7
        first = self._anchor.replace(day=1)
        offset = (first.weekday() - self._layout.first_day_of_week) % 7
        return first - timedelta(days=offset), 42

    def _provider_label(self) -> str:
        try:
            definitions = self.controller.calendar_provider_definitions()
            definition = next(
                item for item in definitions if item.provider_id == self._layout.provider_id
            )
            source = definition.source_label
        except (KeyError, StopIteration):
            source = self._layout.provider_id
        calendars = (
            "Hidden by privacy settings"
            if self._hide_sensitive
            else ", ".join(self._layout.calendar_ids)
        )
        return f"{source}  •  {calendars}"

    def _extension_id(self) -> str | None:
        try:
            definition = next(
                item
                for item in self.controller.calendar_provider_definitions()
                if item.provider_id == self._layout.provider_id
            )
        except (KeyError, StopIteration):
            return None
        return definition.extension_id

    def _request_extension(self) -> None:
        extension_id = self._extension_id()
        if extension_id is not None:
            self.extension_requested.emit(extension_id)

    def _range_label(self) -> str:
        start_date, day_count = self._visible_dates()
        end_date = start_date + timedelta(days=day_count - 1)
        if self._layout.view == CalendarView.MONTH:
            return self._anchor.strftime("%B %Y")
        if start_date == end_date:
            return f"{_month_day(start_date)}, {start_date.year}"
        return f"{_month_day(start_date)} – {_month_day(end_date)}, {end_date.year}"

    def _move(self, direction: int) -> None:
        if self._layout.view == CalendarView.MONTH:
            month = self._anchor.month - 1 + direction
            year = self._anchor.year + month // 12
            month = month % 12 + 1
            day = min(self._anchor.day, _days_in_month(year, month))
            self._anchor = date(year, month, day)
        elif self._layout.view == CalendarView.WEEK:
            self._anchor += timedelta(days=7 * direction)
        else:
            self._anchor += timedelta(days=self._layout.horizon_days * direction)
        self._invalidate_snapshot()
        self.range_label.setText(self._range_label())
        self._render()
        self.range_changed.emit()

    def _today(self) -> None:
        self._anchor = datetime.now().astimezone().date()
        self._invalidate_snapshot()
        self.range_label.setText(self._range_label())
        self._render()
        self.range_changed.emit()

    def _view_changed(self) -> None:
        value = self.view_input.currentData()
        if not isinstance(value, str):
            return
        self._layout = self._layout.model_copy(update={"view": CalendarView(value)})
        self._invalidate_snapshot()
        self.range_label.setText(self._range_label())
        self.provider_label.setText(self._provider_label())
        self.layout_changed.emit(self._layout)
        self._render()
        self.range_changed.emit()

    def _provider_changed(self) -> None:
        value = self.provider_input.currentData()
        if not isinstance(value, str) or value == self._layout.provider_id:
            return
        self._layout = self._layout.model_copy(
            update={"provider_id": value, "calendar_ids": ("primary",)}
        )
        self._invalidate_snapshot()
        self.provider_label.setText(self._provider_label())
        self.layout_changed.emit(self._layout)
        self._render()
        self.range_changed.emit()

    def _display_preferences_changed(self) -> None:
        self._layout = self._layout.model_copy(
            update={
                "show_weekends": self.weekends_input.isChecked(),
                "show_declined": self.declined_input.isChecked(),
            }
        )
        self._invalidate_snapshot()
        self.layout_changed.emit(self._layout)
        self._render()
        self.range_changed.emit()

    def _invalidate_snapshot(self) -> None:
        self._snapshot = None
        self.status_label.setText("Not synced")
        self.setProperty("calendarState", "")
        self.style().unpolish(self)
        self.style().polish(self)

    def _set_state(self, state: CalendarState) -> None:
        self.setProperty("calendarState", state.value)
        self.style().unpolish(self)
        self.style().polish(self)

    def _status_text(self, snapshot: CalendarSnapshot) -> str:
        if snapshot.state == CalendarState.READY:
            return f"{len(snapshot.events)} event(s)  •  Synced {snapshot.collected_at.astimezone().strftime('%H:%M:%S')}"
        if snapshot.state == CalendarState.EMPTY:
            return f"No events  •  Synced {snapshot.collected_at.astimezone().strftime('%H:%M:%S')}"
        return snapshot.summary or snapshot.state.value.replace("_", " ").title()

    def _render(self) -> None:
        _clear_layout(self.body_layout)
        if self._loading:
            loading = QLabel("Loading calendar events…")
            loading.setTextFormat(Qt.TextFormat.PlainText)
            loading.setObjectName("calendarPlaceholder")
            loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.body_layout.addWidget(loading, 0, 0)
            return
        if self._snapshot is not None and self._snapshot.state not in {
            CalendarState.READY,
            CalendarState.EMPTY,
            CalendarState.STALE,
            CalendarState.OFFLINE,
        }:
            placeholder = QLabel(self._snapshot.summary or "Calendar events are unavailable.")
            placeholder.setTextFormat(Qt.TextFormat.PlainText)
            placeholder.setObjectName("calendarPlaceholder")
            placeholder.setWordWrap(True)
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.body_layout.addWidget(placeholder, 0, 0)
            return
        events = self._snapshot.events if self._snapshot is not None else ()
        if self._layout.view == CalendarView.AGENDA:
            self._render_agenda(events)
        elif self._layout.view == CalendarView.WEEK:
            self._render_week(events)
        else:
            self._render_month(events)

    def _render_month(self, events: tuple[CalendarEvent, ...]) -> None:
        start, _ = self._visible_dates()
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        for column in range(7):
            index = (self._layout.first_day_of_week + column) % 7
            header = QLabel(names[index])
            header.setTextFormat(Qt.TextFormat.PlainText)
            header.setObjectName("calendarWeekday")
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setVisible(self._layout.show_weekends or index < 5)
            self.body_layout.addWidget(header, 0, column)
        for offset in range(42):
            current = start + timedelta(days=offset)
            row, column = offset // 7 + 1, offset % 7
            cell = self._day_cell(current, events, current.month != self._anchor.month)
            cell.setVisible(self._layout.show_weekends or current.weekday() < 5)
            self.body_layout.addWidget(cell, row, column)
        for column in range(7):
            index = (self._layout.first_day_of_week + column) % 7
            self.body_layout.setColumnStretch(
                column, 1 if self._layout.show_weekends or index < 5 else 0
            )
        for row in range(1, 7):
            self.body_layout.setRowStretch(row, 1)

    def _render_week(self, events: tuple[CalendarEvent, ...]) -> None:
        start, _ = self._visible_dates()
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        for column in range(7):
            current = start + timedelta(days=column)
            index = (self._layout.first_day_of_week + column) % 7
            header = QLabel(f"{names[index]}\n{_month_day(current)}")
            header.setTextFormat(Qt.TextFormat.PlainText)
            header.setObjectName("calendarWeekday")
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setVisible(self._layout.show_weekends or current.weekday() < 5)
            self.body_layout.addWidget(header, 0, column)
            cell = self._day_cell(current, events, False)
            cell.setVisible(self._layout.show_weekends or current.weekday() < 5)
            self.body_layout.addWidget(cell, 1, column)
            self.body_layout.setColumnStretch(
                column,
                1 if self._layout.show_weekends or current.weekday() < 5 else 0,
            )
        self.body_layout.setRowStretch(1, 1)

    def _render_agenda(self, events: tuple[CalendarEvent, ...]) -> None:
        start, day_count = self._visible_dates()
        visible = [
            event
            for event in events
            if any(
                _event_on_date(event, start + timedelta(days=offset)) for offset in range(day_count)
            )
            and (
                self._layout.show_weekends
                or any(
                    _event_on_date(event, start + timedelta(days=offset))
                    and (start + timedelta(days=offset)).weekday() < 5
                    for offset in range(day_count)
                )
            )
        ]
        if not visible:
            empty = QLabel("No events in the selected range.")
            empty.setTextFormat(Qt.TextFormat.PlainText)
            empty.setObjectName("calendarPlaceholder")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.body_layout.addWidget(empty, 0, 0)
            return
        for row, event in enumerate(visible):
            when = _event_when(event)
            label = QLabel(f"{when}   {self._event_title(event)}")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setObjectName("calendarAgendaItem")
            label.setWordWrap(True)
            label.setToolTip(self._event_tooltip(event))
            self.body_layout.addWidget(label, row, 0)
        self.body_layout.setColumnStretch(0, 1)

    def _day_cell(
        self,
        current: date,
        events: tuple[CalendarEvent, ...],
        outside_month: bool,
    ) -> QFrame:
        cell = QFrame()
        cell.setObjectName("calendarDayCell")
        cell.setProperty("outsideMonth", outside_month)
        cell.setProperty("today", current == datetime.now().astimezone().date())
        cell_layout = QVBoxLayout(cell)
        cell_layout.setContentsMargins(7, 6, 7, 6)
        cell_layout.setSpacing(3)
        day_label = QLabel(str(current.day))
        day_label.setTextFormat(Qt.TextFormat.PlainText)
        day_label.setObjectName("calendarDayNumber")
        day_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        cell_layout.addWidget(day_label)
        day_events = [event for event in events if _event_on_date(event, current)]
        for event in day_events[:4]:
            chip = QLabel(self._event_text(event))
            chip.setTextFormat(Qt.TextFormat.PlainText)
            chip.setObjectName("calendarEventChip")
            chip.setWordWrap(False)
            chip.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            chip.setToolTip(self._event_tooltip(event))
            cell_layout.addWidget(chip)
        if len(day_events) > 4:
            more = QLabel(f"+{len(day_events) - 4} more")
            more.setTextFormat(Qt.TextFormat.PlainText)
            more.setObjectName("calendarMoreEvents")
            cell_layout.addWidget(more)
        cell_layout.addStretch()
        return cell

    def _event_text(self, event: CalendarEvent) -> str:
        if self._event_is_hidden(event):
            return "Busy"
        if event.all_day:
            return event.title
        return f"{event.start_at.astimezone().strftime('%H:%M')}  {event.title}"

    def _event_title(self, event: CalendarEvent) -> str:
        return "Busy" if self._event_is_hidden(event) else event.title

    def _event_tooltip(self, event: CalendarEvent) -> str:
        if self._event_is_hidden(event):
            return "Calendar event details are hidden by privacy settings."
        details = [escape(self._event_title(event))]
        if event.location:
            details.append(escape(event.location))
        return " • ".join(details)

    def _event_is_hidden(self, event: CalendarEvent) -> bool:
        return self._hide_sensitive and event.sensitive


def _clear_layout(layout: QGridLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        child = item.layout()
        if child is not None:
            _clear_layout(child)  # type: ignore[arg-type]


def _days_in_month(year: int, month: int) -> int:
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (next_month - date(year, month, 1)).days


def _event_on_date(event: CalendarEvent, current: date) -> bool:
    if event.all_day:
        assert event.start_date is not None and event.end_date is not None
        return event.start_date <= current < event.end_date
    assert event.start_at is not None and event.end_at is not None
    local_zone = datetime.now().astimezone().tzinfo
    day_start = datetime.combine(current, time.min, tzinfo=local_zone)
    day_end = day_start + timedelta(days=1)
    return event.start_at.astimezone() < day_end and event.end_at.astimezone() > day_start


def _event_when(event: CalendarEvent) -> str:
    if event.all_day:
        assert event.start_date is not None
        return f"{event.start_date.strftime('%a')}, {_month_day(event.start_date)}"
    assert event.start_at is not None
    local = event.start_at.astimezone()
    return f"{local.strftime('%a')}, {_month_day(local.date())} {local.strftime('%H:%M')}"


def _month_day(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}"
