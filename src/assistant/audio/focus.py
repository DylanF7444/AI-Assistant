from __future__ import annotations

import threading
from collections.abc import Callable


class AudioFocusCoordinator:
    """Aggregate named speaker-output sources into one capture suppression state."""

    def __init__(
        self,
        playback_state_changed: Callable[[bool], None] | None = None,
    ) -> None:
        self._playback_state_changed = playback_state_changed
        self._active_outputs: set[str] = set()
        self._lock = threading.RLock()

    @property
    def output_active(self) -> bool:
        with self._lock:
            return bool(self._active_outputs)

    @property
    def active_outputs(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._active_outputs))

    def set_output_active(self, source: str, active: bool) -> None:
        normalized = source.strip()
        if not normalized:
            raise ValueError("Audio focus sources must have a name")
        with self._lock:
            was_active = bool(self._active_outputs)
            if active:
                self._active_outputs.add(normalized)
            else:
                self._active_outputs.discard(normalized)
            is_active = bool(self._active_outputs)
        if is_active != was_active and self._playback_state_changed is not None:
            self._playback_state_changed(is_active)

    def output_callback(self, source: str) -> Callable[[bool], None]:
        normalized = source.strip()
        if not normalized:
            raise ValueError("Audio focus sources must have a name")

        def update(active: bool) -> None:
            self.set_output_active(normalized, active)

        return update

    def clear(self) -> None:
        with self._lock:
            was_active = bool(self._active_outputs)
            self._active_outputs.clear()
        if was_active and self._playback_state_changed is not None:
            self._playback_state_changed(False)
