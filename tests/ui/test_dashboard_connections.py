from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.bootstrap import build_container
from assistant.ui.desktop.controller import DesktopController
from assistant.ui.desktop.dashboard_connections import DashboardConnectionsDialog
from assistant.ui.desktop.dashboard_editor import DashboardEditor


@pytest.mark.ui
def test_home_assistant_connection_appears_in_editor_and_requires_exact_entity(qtbot, settings):
    from PySide6.QtWidgets import QMessageBox

    controller = DesktopController(build_container(settings))
    controller.container.dashboard.set_offline_lock(False)
    provider = controller.container.dashboard.registry.provider("home_assistant")
    provider.adapter_factory = _FakeHomeAssistantAdapter
    connection = asyncio.run(
        controller.add_home_assistant_connection(
            display_name="My Home",
            endpoint="https://home.example.com",
            token="secret-token",
        )
    )

    editor = DashboardEditor(controller)
    qtbot.addWidget(editor)
    editor.show()
    metric_index = editor.metric_catalog.findData("home_assistant.entity_state")
    editor.metric_catalog.setCurrentIndex(metric_index)
    editor._add_card()
    editor.card_list.setCurrentRow(editor.card_list.count() - 1)

    assert editor.connection_input.currentData() == str(connection.connection_id)
    assert editor.entity_input.isVisible()
    warnings = []
    original_warning = QMessageBox.warning
    QMessageBox.warning = lambda *args: warnings.append(str(args[2]))
    try:
        editor._accept()
        assert warnings
        assert "entity ID" in warnings[-1]
        editor.entity_input.setText("sensor.office_temperature")
        editor.entity_input.editingFinished.emit()
        editor.attribute_input.setText("temperature")
        editor.attribute_input.editingFinished.emit()
        editor._accept()
    finally:
        QMessageBox.warning = original_warning

    layout = editor.selected_layout()
    card = next(card for card in layout.cards if card.metric_id == "home_assistant.entity_state")
    assert card.connection_id == connection.connection_id
    assert card.options == {
        "entity_id": "sensor.office_temperature",
        "attribute": "temperature",
    }

    dialog = DashboardConnectionsDialog(controller)
    qtbot.addWidget(dialog)
    assert dialog.connection_list.count() == 1
    assert "secret-token" not in dialog.connection_detail.text()
    history_requests = []
    dialog.history_requested.connect(history_requests.append)
    dialog.history_button.click()
    assert history_requests == [(connection.connection_id, True)]

    updated = controller.set_dashboard_connection_history_enabled(
        connection.connection_id,
        True,
    )
    dialog.reload_connections(updated.connection_id)
    assert dialog.history_button.text() == "Disable history"
    assert controller.clear_dashboard_connection_history(connection.connection_id) == 0


class _FakeHomeAssistantAdapter:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token

    async def health(self) -> dict:
        return {"healthy": True}

    async def state(self, entity_id: str) -> dict:
        return {
            "state": "21",
            "attributes": {"friendly_name": entity_id},
        }
