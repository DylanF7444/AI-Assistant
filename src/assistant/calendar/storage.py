from __future__ import annotations

from pydantic import ValidationError

from assistant.calendar.contracts import CalendarLayout
from assistant.storage.database import SettingsStore


class CalendarLayoutStore:
    KEY = "calendar.layout.v1"

    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings

    def load(self) -> CalendarLayout:
        payload = self.settings.get(self.KEY)
        if payload is None:
            layout = CalendarLayout()
            self.save(layout)
            return layout
        try:
            return CalendarLayout.model_validate(payload)
        except (TypeError, ValidationError, ValueError):
            return CalendarLayout()

    def save(self, layout: CalendarLayout) -> CalendarLayout:
        validated = CalendarLayout.model_validate(layout)
        self.settings.set(self.KEY, validated.model_dump(mode="json"))
        return validated
