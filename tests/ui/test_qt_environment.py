from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QLabel


@pytest.mark.ui
def test_qt_test_environment_runs_offscreen(qtbot):
    label = QLabel("Local Assistant")
    qtbot.addWidget(label)

    label.show()

    assert label.text() == "Local Assistant"
    assert label.isVisible()
