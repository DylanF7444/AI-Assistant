from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLineEdit

from assistant.bootstrap import build_container
from assistant.command_center import (
    DeliveryMode,
    DestinationBinding,
    SourceBinding,
    SourceUseMode,
    WorkBrief,
)
from assistant.config import Settings
from assistant.contracts import ResultStatus
from assistant.ui.desktop.connections import (
    ConnectionsPage,
    _ArtifactDialog,
    _BindingDialog,
    _ScheduleBriefDialog,
)
from assistant.ui.desktop.controller import DesktopController


@pytest.mark.ui
def test_connections_page_adds_schema_validated_input_and_output(qtbot, tmp_path):
    controller = DesktopController(build_container(Settings(profile="testing", data_dir=tmp_path)))

    source_dialog = _BindingDialog(controller, "source")
    qtbot.addWidget(source_dialog)
    source_dialog.definition_input.setCurrentIndex(
        source_dialog.definition_input.findData("web.page")
    )
    source_dialog.name_input.setText("Project status")
    url_input = source_dialog.configuration_form._fields["url"][1]
    assert isinstance(url_input, QLineEdit)
    url_input.setText("https://example.com/status")
    source = controller.save_command_center_source(source_dialog.value())

    destination_dialog = _BindingDialog(controller, "destination")
    qtbot.addWidget(destination_dialog)
    destination_dialog.definition_input.setCurrentIndex(
        destination_dialog.definition_input.findData("local.notification")
    )
    destination_dialog.name_input.setText("Project notification")
    destination = controller.save_command_center_destination(destination_dialog.value())

    page = ConnectionsPage(controller)
    qtbot.addWidget(page)
    page.show()

    assert page.source_table.rowCount() == 1
    assert page.source_table.item(0, 0).text() == "Project status"
    assert page.source_table.item(0, 4).text() == "Available"
    assert page.destination_table.rowCount() == 1
    assert page.destination_table.item(0, 0).text() == "Project notification"
    assert page.destination_table.item(0, 3).text() == "Local"
    assert page._selected_id(page.source_table) is None
    page.source_table.selectRow(0)
    assert page._selected_id(page.source_table) == source.binding_id
    page.destination_table.selectRow(0)
    assert page._selected_id(page.destination_table) == destination.binding_id


@pytest.mark.ui
def test_connections_page_uses_extension_hub_for_accounts_and_non_secret_picker(
    qtbot,
    tmp_path,
):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    asyncio.run(container.extension_hub.install("integration.gmail"))
    profile = container.connection_profiles.create(
        provider_id="google",
        display_name="Work Google",
        account="person@example.com",
    )
    page = ConnectionsPage(DesktopController(container))
    qtbot.addWidget(page)
    dialog = _BindingDialog(DesktopController(container), "source")
    qtbot.addWidget(dialog)
    dialog.definition_input.setCurrentIndex(dialog.definition_input.findData("gmail.unread"))

    rendered = " ".join(
        dialog.account_input.itemText(index) for index in range(dialog.account_input.count())
    )
    assert "Work Google" in rendered
    assert "person@example.com" in rendered
    assert str(profile.profile_id) not in rendered
    assert [page.tabs.tabText(index) for index in range(page.tabs.count())] == [
        "Inputs",
        "Outputs",
        "Reviewed work",
    ]


@pytest.mark.ui
def test_connections_page_routes_account_management_to_extension_hub(qtbot, tmp_path):
    controller = DesktopController(build_container(Settings(profile="testing", data_dir=tmp_path)))
    page = ConnectionsPage(controller)
    qtbot.addWidget(page)
    requested: list[str] = []
    page.extension_hub_requested.connect(requested.append)

    page.extension_hub_button.click()

    assert requested == [""]


@pytest.mark.ui
def test_new_bindings_hide_uninstalled_extensions_until_hub_install(qtbot, tmp_path):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    controller = DesktopController(container)

    before = _BindingDialog(controller, "source")
    qtbot.addWidget(before)

    assert before.definition_input.findData("github.repository_issues") == -1
    assert before.definition_input.findData("web.page") >= 0
    assert controller.command_center_extension_id("core.tasks") == ""
    browse_requests = []
    before.extension_hub_requested.connect(browse_requests.append)
    before.browse_extensions_button.click()
    assert browse_requests == [""]

    asyncio.run(container.extension_hub.install("integration.github"))
    after = _BindingDialog(controller, "source")
    qtbot.addWidget(after)
    github_index = after.definition_input.findData("github.repository_issues")
    requested = []
    after.extension_hub_requested.connect(requested.append)

    assert github_index >= 0
    assert "Disabled" in after.definition_input.itemText(github_index)
    after.definition_input.setCurrentIndex(github_index)
    after.manage_account_button.click()
    assert requested == ["integration.github"]


@pytest.mark.ui
def test_binding_editor_uses_declared_schema_instead_of_operation_json(qtbot, tmp_path):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    asyncio.run(container.extension_hub.install("integration.github"))
    controller = DesktopController(container)
    dialog = _BindingDialog(controller, "source")
    qtbot.addWidget(dialog)
    dialog.definition_input.setCurrentIndex(
        dialog.definition_input.findData("github.repository_issues")
    )

    assert set(dialog.configuration_form._fields) == {"owner", "repo", "max_results"}
    assert "operation" not in dialog.configuration_form._fields
    for name in ("owner", "repo"):
        widget = dialog.configuration_form._fields[name][1]
        assert isinstance(widget, QLineEdit)
        widget.setText("example")
    binding = dialog.value()

    assert binding.configuration == {
        "owner": "example",
        "repo": "example",
        "max_results": 25,
    }
    assert binding.connection is not None
    assert binding.connection.provider_id == "github"
    assert binding.connection.profile_id is None
    assert dialog.account_input.itemData(0, Qt.ItemDataRole.UserRole) == ""


@pytest.mark.ui
def test_connections_page_reviews_revisioned_artifacts_and_exact_delivery_targets(
    qtbot,
    tmp_path,
):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    artifact = container.command_center.save_brief(
        WorkBrief(
            status=ResultStatus.COMPLETED,
            content="# Work Brief\n\nReviewed content.",
        )
    )
    destination = container.command_center.save_destination(
        DestinationBinding(
            destination_id="local.inbox",
            display_name="Inbox",
            delivery_mode=DeliveryMode.LOCAL,
            allow_sensitive=True,
        )
    )
    pending = container.command_center.queue_delivery(
        artifact.artifact_id,
        destination.binding_id,
    )
    controller = DesktopController(container)
    page = ConnectionsPage(controller)
    qtbot.addWidget(page)

    assert page.tabs.tabText(2) == "Reviewed work"
    assert page.artifact_table.rowCount() == 1
    assert page.artifact_table.item(0, 0).text() == "Work Brief"
    assert page.delivery_table.item(0, 1).text() == "Local Assistant Inbox"
    assert page.delivery_table.item(0, 2).text() == "Pending"

    editor = _ArtifactDialog(artifact)
    qtbot.addWidget(editor)
    editor.title_input.setText("Reviewed title")
    editor.body_input.setPlainText("Reviewed revision two.")
    revised = controller.edit_command_center_artifact(
        artifact.artifact_id,
        expected_revision=artifact.revision,
        title=editor.title_input.text(),
        body=editor.body_input.toPlainText(),
    )
    page.reload()

    assert revised.revision == 2
    assert page.artifact_table.item(0, 0).text() == "Reviewed title"
    assert page.delivery_table.item(0, 2).text() == "Invalidated"
    assert page._selected_id(page.delivery_table) is None
    assert pending.delivery_id == container.command_center.pending_deliveries()[0].delivery_id


@pytest.mark.ui
def test_connections_page_emits_explicit_build_and_delivery_review_actions(qtbot, tmp_path):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    page = ConnectionsPage(DesktopController(container))
    qtbot.addWidget(page)
    work_requests = []
    automation_requests = []
    delivery_requests = []
    page.work_brief_requested.connect(lambda: work_requests.append(True))
    page.automation_requested.connect(automation_requests.append)
    page.delivery_prepare_requested.connect(delivery_requests.append)

    page.work_brief_requested.emit()
    page.automations_button.click()

    assert work_requests == [True]
    assert automation_requests == [None]
    assert delivery_requests == []


@pytest.mark.ui
def test_connections_page_creates_automation_then_emits_exact_schedule(
    qtbot,
    tmp_path,
    monkeypatch,
):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    container.command_center.save_source(
        SourceBinding(
            source_id="local.tasks",
            display_name="My tasks",
            configuration={},
            use_modes=(SourceUseMode.BRIEFING,),
        )
    )
    page = ConnectionsPage(DesktopController(container))
    qtbot.addWidget(page)
    requested = []
    page.automation_requested.connect(requested.append)
    monkeypatch.setattr(
        _ScheduleBriefDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )

    page.create_work_brief_automation()

    schedules = container.schedule_store.list()
    assert len(schedules) == 1
    assert requested == [schedules[0].schedule_id]


@pytest.mark.ui
def test_schedule_brief_dialog_creates_a_frozen_recurring_automation(qtbot, tmp_path):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    container.command_center.save_source(
        SourceBinding(
            source_id="local.tasks",
            display_name="My tasks",
            configuration={},
            use_modes=(SourceUseMode.BRIEFING,),
        )
    )
    controller = DesktopController(container)
    dialog = _ScheduleBriefDialog(controller)
    qtbot.addWidget(dialog)
    dialog.name_input.setText("Morning brief")
    dialog.title_input.setText("Morning Work Brief")

    schedule = controller.schedule_command_center_brief(**dialog.values())

    assert schedule.name == "Morning brief"
    assert schedule.action.kind == "command_center_brief"
    assert schedule.action.title == "Morning Work Brief"
    assert schedule.trigger.kind == "recurrence"
    assert schedule.trigger.frequency == "daily"
    assert schedule.action.sources[0].call.caller == "scheduler"
