from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from assistant.contracts import PermissionContext, ResultStatus, RiskLevel, SkillCall
from assistant.skills.windows import (
    ApplicationWindowSkill,
    FileOperationSkill,
    NotificationSkill,
    ProcessSkill,
    InputAutomationSkill,
    WindowsSettingSkill,
)
from assistant.windows.adapters import ProcessInfo


class FakeFiles:
    def __init__(self):
        self.deleted = []
        self.opened = []

    async def search(self, root, query, limit):
        return []

    async def copy(self, source, destination):
        return Path(shutil.copy2(source, destination))

    async def move(self, source, destination):
        return Path(shutil.move(source, destination))

    async def delete_to_recycle_bin(self, path):
        self.deleted.append(Path(path))
        Path(path).unlink(missing_ok=True)

    async def open(self, path):
        self.opened.append(Path(path))


class FakeApps:
    def __init__(self):
        self.calls = []

    async def launch(self, registered_path):
        self.calls.append(("launch", registered_path))

    async def focus(self, process_name):
        self.calls.append(("focus", process_name))
        return True

    async def minimize(self, process_name):
        self.calls.append(("minimize", process_name))
        return True

    async def maximize(self, process_name):
        self.calls.append(("maximize", process_name))
        return True

    async def close(self, process_name):
        self.calls.append(("close", process_name))
        return True


class FakeNotifications:
    def __init__(self):
        self.value = None

    async def show(self, title, message):
        self.value = (title, message)


class FakeProcesses:
    def __init__(self):
        self.terminated = None

    async def list(self, name_filter=None):
        return [ProcessInfo("test.exe", 42, "Console", "1,024 K")]

    async def terminate(self, pid):
        self.terminated = pid
        return True


class FakeInput:
    def __init__(self, active="approved.exe", sensitive=False):
        self.active = active
        self.sensitive = sensitive
        self.typed = None
        self.clicked = None

    async def active_process_name(self):
        return self.active

    async def focused_control_is_sensitive(self):
        return self.sensitive

    async def type_text(self, text):
        self.typed = text

    async def click(self, x, y):
        self.clicked = (x, y)


class FakeSettings:
    def __init__(self):
        self.dark = False

    async def get_dark_mode(self):
        return self.dark

    async def set_dark_mode(self, enabled):
        self.dark = enabled


def call(skill_id, arguments):
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


@pytest.mark.asyncio
async def test_file_dry_run_and_scoped_targets(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("data")
    destination = tmp_path / "destination.txt"
    skill = FileOperationSkill(FakeFiles())
    skill_call = call(
        "windows.file_operation",
        {
            "operation": "copy",
            "source": str(source),
            "destination": str(destination),
            "dry_run": True,
        },
    )
    result = await skill.execute(skill_call, __import__("asyncio").Event())
    targets = skill.required_permissions(skill_call)[0].targets
    assert result.status == ResultStatus.COMPLETED
    assert not destination.exists()
    assert str(source.resolve()) in targets
    assert str(destination.resolve()) in targets
    assert skill.risk_for(skill_call) == RiskLevel.MEDIUM


@pytest.mark.asyncio
async def test_file_copy_and_reliable_rollback(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("data")
    destination = tmp_path / "destination.txt"
    adapter = FakeFiles()
    skill = FileOperationSkill(adapter)
    result = await skill.execute(
        call(
            "windows.file_operation",
            {
                "operation": "copy",
                "source": str(source),
                "destination": str(destination),
            },
        ),
        __import__("asyncio").Event(),
    )
    assert destination.read_text() == "data"
    rollback = await skill.rollback(result.rollback_token)
    assert rollback.status == ResultStatus.COMPLETED
    assert not destination.exists()


@pytest.mark.asyncio
async def test_window_notification_and_process_adapters_are_typed():
    apps = FakeApps()
    window = ApplicationWindowSkill(apps)
    window_call = call(
        "windows.application_window",
        {"operation": "maximize", "process_name": "test.exe"},
    )
    assert (await window.execute(window_call, __import__("asyncio").Event())).status == ResultStatus.COMPLETED
    assert window.risk_for(window_call) == RiskLevel.MEDIUM

    notifications = FakeNotifications()
    notification = NotificationSkill(notifications)
    await notification.execute(
        call(
            "windows.notification",
            {"title": "Title", "message": "Message"},
        ),
        __import__("asyncio").Event(),
    )
    assert notifications.value == ("Title", "Message")

    processes = FakeProcesses()
    process = ProcessSkill(processes)
    list_call = call("windows.process", {"operation": "list", "name_filter": "test"})
    listed = await process.execute(list_call, __import__("asyncio").Event())
    assert listed.data["processes"][0]["pid"] == 42
    terminate_call = call("windows.process", {"operation": "terminate", "pid": 42})
    assert process.risk_for(terminate_call) == RiskLevel.CRITICAL
    await process.execute(terminate_call, __import__("asyncio").Event())
    assert processes.terminated == 42


@pytest.mark.asyncio
async def test_input_automation_requires_active_approved_app_and_safe_field():
    adapter = FakeInput()
    skill = InputAutomationSkill(adapter)
    skill_call = call(
        "windows.input_automation",
        {
            "operation": "type_text",
            "application": "approved.exe",
            "text": "private text",
        },
    )
    preview = await skill.preview(skill_call)
    assert "private text" not in preview
    await skill.execute(skill_call, __import__("asyncio").Event())
    assert adapter.typed == "private text"
    adapter.active = "other.exe"
    with pytest.raises(PermissionError, match="Active application"):
        await skill.execute(skill_call, __import__("asyncio").Event())
    adapter.active = "approved.exe"
    adapter.sensitive = True
    with pytest.raises(PermissionError, match="sensitive"):
        await skill.execute(skill_call, __import__("asyncio").Event())


@pytest.mark.asyncio
async def test_supported_windows_setting_is_explicit_and_typed():
    adapter = FakeSettings()
    skill = WindowsSettingSkill(adapter)
    setting_call = call(
        "windows.setting",
        {"operation": "set", "setting": "dark_mode", "enabled": True},
    )
    result = await skill.execute(setting_call, __import__("asyncio").Event())
    assert result.data["enabled"] is True
    assert skill.required_permissions(setting_call)[0].capability == "settings.set"
