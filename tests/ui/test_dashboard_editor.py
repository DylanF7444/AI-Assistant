from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.bootstrap import build_container
from assistant.ui.desktop.controller import DesktopController
from assistant.ui.desktop.dashboard_editor import DashboardEditor


@pytest.mark.ui
def test_dashboard_editor_adds_configures_moves_and_stages_layout(qtbot, settings):
    from PySide6.QtCore import Qt

    controller = DesktopController(build_container(settings))
    original = controller.dashboard_layout()
    editor = DashboardEditor(controller)
    qtbot.addWidget(editor)
    editor.show()

    assert editor.metric_catalog.isEditable()
    qtbot.mouseClick(editor.add_button, Qt.MouseButton.LeftButton)
    assert editor.card_list.count() == len(original.cards) + 1
    assert controller.dashboard_layout() == original

    editor.card_list.setCurrentRow(editor.card_list.count() - 1)
    editor.title_input.setText("My custom metric")
    editor.title_input.editingFinished.emit()
    editor.size_input.setCurrentIndex(editor.size_input.findData("wide"))
    editor.refresh_input.setCurrentIndex(editor.refresh_input.findData(300))
    editor.enabled_input.setChecked(False)
    qtbot.mouseClick(editor.move_up_button, Qt.MouseButton.LeftButton)
    editor._accept()

    customized = editor.selected_layout()
    custom_card = next(card for card in customized.cards if card.title == "My custom metric")
    assert custom_card.size == "wide"
    assert custom_card.refresh_seconds == 300
    assert not custom_card.enabled
    assert custom_card.position == len(customized.cards) - 2

    controller.save_dashboard_layout(customized)
    assert controller.dashboard_layout() == customized


@pytest.mark.ui
def test_dashboard_editor_reset_is_cancel_safe(qtbot, settings):
    from PySide6.QtCore import Qt

    controller = DesktopController(build_container(settings))
    original = controller.dashboard_layout()
    editor = DashboardEditor(controller)
    qtbot.addWidget(editor)

    qtbot.mouseClick(editor.add_button, Qt.MouseButton.LeftButton)
    assert editor.card_list.count() == len(original.cards) + 1
    qtbot.mouseClick(editor.reset_button, Qt.MouseButton.LeftButton)
    assert editor.card_list.count() == len(controller.dashboard_default_layout().cards)
    editor.reject()

    assert controller.dashboard_layout() == original
