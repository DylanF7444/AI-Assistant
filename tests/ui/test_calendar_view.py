from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.calendar.contracts import (
    CalendarEvent,
    CalendarLayout,
    CalendarProviderDefinition,
    CalendarSnapshot,
    CalendarState,
    CalendarView,
)
from assistant.ui.desktop.calendar_view import CalendarLayoutWidget


class _Controller:
    def __init__(self):
        self.layout = CalendarLayout(provider_id="test")

    def calendar_layout(self):
        return self.layout

    def calendar_provider_definitions(self):
        return (
            CalendarProviderDefinition(
                provider_id="test",
                display_name="Test Calendar",
                description="Test provider",
                source_label="Test Calendar",
                remote=False,
                requires_connection=False,
            ),
        )

    def calendar_query(self, *, start, end, layout=None):
        from assistant.calendar.contracts import CalendarQuery

        selected = layout or self.layout
        return CalendarQuery(
            provider_id=selected.provider_id,
            calendar_ids=selected.calendar_ids,
            start=start,
            end=end,
            max_events=selected.max_events,
        )

    def hide_sensitive_display(self):
        return False


@pytest.mark.ui
def test_calendar_layout_renders_provider_events_and_privacy_mask(qtbot):
    controller = _Controller()
    widget = CalendarLayoutWidget(controller)
    qtbot.addWidget(widget)
    widget._anchor = date(2026, 8, 12)
    event = CalendarEvent(
        event_id="event-1",
        calendar_id="primary",
        title="Planning <private>",
        start_at=datetime(2026, 8, 12, 10, tzinfo=UTC),
        end_at=datetime(2026, 8, 12, 11, tzinfo=UTC),
    )
    widget.set_snapshot(
        CalendarSnapshot(
            provider_id="test",
            source_label="Test Calendar",
            range_start=datetime(2026, 8, 1, tzinfo=UTC),
            range_end=datetime(2026, 9, 12, tzinfo=UTC),
            state=CalendarState.READY,
            events=(event,),
        )
    )
    cells = [
        item.widget()
        for index in range(widget.body_layout.count())
        if (item := widget.body_layout.itemAt(index)).widget() is not None
        and item.widget().objectName() == "calendarDayCell"
    ]
    chips = [
        child
        for cell in cells
        for child in cell.findChildren(type(widget.body), "calendarEventChip")
    ]
    assert len(cells) == 42
    assert len(chips) == 1
    assert chips[0].text() == widget._event_text(event)
    assert chips[0].textFormat().name == "PlainText"

    widget.set_sensitive_hidden(True)
    cells = [
        item.widget()
        for index in range(widget.body_layout.count())
        if (item := widget.body_layout.itemAt(index)).widget() is not None
        and item.widget().objectName() == "calendarDayCell"
    ]
    assert (
        next(
            cell.findChildren(type(widget.body), "calendarEventChip")[0]
            for cell in cells
            if cell.findChildren(type(widget.body), "calendarEventChip")
        ).text()
        == "Busy"
    )
    assert "primary" not in widget.provider_label.text()

    widget.view_input.setCurrentIndex(widget.view_input.findData(CalendarView.AGENDA.value))
    assert widget.layout_config.view == CalendarView.AGENDA
