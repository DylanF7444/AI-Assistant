from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant.ui.desktop.state import DesktopWindowState, WindowGeometry


def test_desktop_window_state_defaults_to_pinned_compact_mode():
    state = DesktopWindowState()

    assert state.mode == "compact"
    assert state.primary_screen == "assistant"
    assert state.compact_pinned is True
    assert state.compact_geometry is None
    assert state.full_geometry is None
    assert state.result_geometry is None


def test_desktop_window_state_round_trips_separate_geometries():
    state = DesktopWindowState(
        mode="full",
        primary_screen="dashboard",
        compact_pinned=False,
        compact_geometry=WindowGeometry(x=10, y=20, width=400, height=520),
        full_geometry=WindowGeometry(x=30, y=40, width=1080, height=760),
        result_geometry=WindowGeometry(x=70, y=80, width=920, height=680),
    )

    restored = DesktopWindowState.model_validate(state.model_dump(mode="json"))

    assert restored == state


def test_desktop_window_state_rejects_non_positive_geometry():
    with pytest.raises(ValidationError):
        WindowGeometry(x=0, y=0, width=0, height=520)
