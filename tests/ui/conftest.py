from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _TestSingleInstance:
    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def isolate_desktop_single_instance(monkeypatch):
    """Do not collide with a real Assistant process during offscreen UI tests."""
    monkeypatch.setattr(
        "assistant.ui.desktop.app.acquire_single_instance",
        lambda: _TestSingleInstance(),
    )
