from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from assistant.contracts import PermissionContext, ResultStatus, RiskLevel, SkillCall
from assistant.skills.windows import (
    ApplicationWindowSkill,
    BrowserNavigateSkill,
    ContextNavigationSkill,
    FileOperationSkill,
    WindowInput,
    NotificationSkill,
    ProcessSkill,
    InputAutomationSkill,
    WindowsDeviceControlSkill,
    WindowsSettingSkill,
)
from assistant.windows.adapters import (
    BrowserWindowInfo,
    LocalWindowsDeviceAdapter,
    ProcessInfo,
    WindowsApplicationAdapter,
)


class FakeFiles:
    def __init__(self):
        self.deleted = []
        self.opened = []

    async def search(self, root, query, limit):
        return []

    async def copy(self, source, destination):
        return Path(shutil.copy2(source, destination))

    async def create_text_file(self, path, content):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

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

    async def launch_with_args(self, registered_path, arguments):
        self.calls.append(("launch_with_args", registered_path, arguments))

    async def focus(self, process_name):
        self.calls.append(("focus", process_name))
        return True

    async def minimize(self, process_name):
        self.calls.append(("minimize", process_name))
        return True

    async def maximize(self, process_name):
        self.calls.append(("maximize", process_name))
        return True

    async def minimize_focused(self):
        self.calls.append(("minimize_focused",))
        return True

    async def maximize_focused(self):
        self.calls.append(("maximize_focused",))
        return True

    async def close(self, process_name):
        self.calls.append(("close", process_name))
        return True

    async def move_to_monitor(self, process_name, monitor):
        self.calls.append(("move_to_monitor", process_name, monitor))
        return True

    async def move(self, process_name, x, y):
        self.calls.append(("move", process_name, x, y))
        return True

    async def resize(self, process_name, width, height):
        self.calls.append(("resize", process_name, width, height))
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


class FakeContextNavigation:
    def __init__(self, active=None, visible=None, visible_windows=()):
        self.active = active
        self.visible = visible
        self.visible_windows = tuple(visible_windows)
        self.focused = []
        self.hotkeys = []
        self.typed = []

    async def active_process_name(self):
        return self.active

    async def first_visible_process(self, process_names):
        return self.visible if self.visible in process_names else None

    async def visible_browser_windows(self, process_names):
        if self.visible_windows:
            return tuple(
                window
                for window in self.visible_windows
                if window.process_name in process_names
            )
        if self.visible in process_names:
            return (BrowserWindowInfo(self.visible, private=False),)
        return ()

    async def focus(self, process_name):
        self.focused.append(process_name)
        return True

    async def hotkey(self, keys):
        self.hotkeys.append(keys)

    async def type_text(self, text):
        self.typed.append(text)


class FakeSettings:
    def __init__(self):
        self.dark = False

    async def get_dark_mode(self):
        return self.dark

    async def set_dark_mode(self, enabled):
        self.dark = enabled


class FakeDeviceControl:
    def __init__(self):
        self.calls = []
        self.brightness = 50

    async def search_installed_apps(self, query=None, limit=20):
        self.calls.append(("search_installed_apps", query, limit))
        return [{"name": "Notepad", "publisher": "Microsoft"}]

    async def set_volume(self, level):
        self.calls.append(("set_volume", level))

    async def change_volume(self, delta):
        self.calls.append(("change_volume", delta))

    async def mute_audio(self, muted):
        self.calls.append(("mute_audio", muted))

    async def set_brightness(self, level):
        self.calls.append(("set_brightness", level))
        self.brightness = level

    async def change_brightness(self, delta):
        self.calls.append(("change_brightness", delta))
        self.brightness += delta
        return self.brightness

    async def set_wifi_enabled(self, enabled):
        self.calls.append(("set_wifi_enabled", enabled))
        return True if enabled is None else enabled

    async def connect_wifi_network(self, ssid):
        self.calls.append(("connect_wifi_network", ssid))

    async def disconnect_wifi_network(self, ssid=None):
        self.calls.append(("disconnect_wifi_network", ssid))

    async def set_bluetooth_enabled(self, enabled):
        self.calls.append(("set_bluetooth_enabled", enabled))
        return False if enabled is None else enabled

    async def connect_bluetooth_device(self, device_name):
        self.calls.append(("connect_bluetooth_device", device_name))

    async def disconnect_bluetooth_device(self, device_name):
        self.calls.append(("disconnect_bluetooth_device", device_name))

    async def lock_device(self):
        self.calls.append(("lock_device",))

    async def restart_device(self):
        self.calls.append(("restart_device",))

    async def shutdown_device(self):
        self.calls.append(("shutdown_device",))

    async def battery_status(self):
        self.calls.append(("battery_status",))
        return {"percent": 80}

    async def storage_status(self, drive=None):
        self.calls.append(("storage_status", drive))
        return [{"drive": drive or "C:\\", "free_bytes": 1}]

    async def performance_status(self):
        self.calls.append(("performance_status",))
        return {"cpu_percent": 10, "memory": {"percent_used": 40}}

    async def connected_devices(self, limit=50):
        self.calls.append(("connected_devices", limit))
        return [{"name": "Keyboard", "status": "OK"}]

    async def set_connected_device_enabled(self, instance_id, enabled):
        self.calls.append(("set_connected_device_enabled", instance_id, enabled))


@pytest.mark.asyncio
async def test_local_wifi_toggle_explains_admin_requirement(monkeypatch):
    async def fake_json_powershell(script):
        return {"Name": "Wi-Fi", "Status": "Up"}

    async def fake_powershell(script):
        raise RuntimeError("Access is denied.")

    monkeypatch.setattr(
        LocalWindowsDeviceAdapter,
        "_json_powershell",
        staticmethod(fake_json_powershell),
    )
    monkeypatch.setattr(
        LocalWindowsDeviceAdapter, "_powershell", staticmethod(fake_powershell)
    )

    with pytest.raises(RuntimeError, match="administrator privileges"):
        await LocalWindowsDeviceAdapter().set_wifi_enabled(False)


@pytest.mark.asyncio
async def test_local_bluetooth_toggle_targets_radio_not_profile_device(monkeypatch):
    commands = []

    async def fake_json_powershell(script):
        return [
            {
                "FriendlyName": "Bluetooth A2DP Source",
                "InstanceId": r"BTHENUM\{0000110E-0000-1000-8000-00805F9B34FB}_LOCALMFG&005D\8&1098DC82&0&DEVICE",
                "Status": "OK",
            },
            {
                "FriendlyName": "Intel(R) Wireless Bluetooth(R)",
                "InstanceId": r"USB\VID_8087&PID_0033\5&2C2A",
                "Status": "OK",
            },
        ]

    async def fake_powershell(script):
        commands.append(script)
        return ""

    monkeypatch.setattr(
        LocalWindowsDeviceAdapter,
        "_json_powershell",
        staticmethod(fake_json_powershell),
    )
    monkeypatch.setattr(
        LocalWindowsDeviceAdapter, "_powershell", staticmethod(fake_powershell)
    )

    enabled = await LocalWindowsDeviceAdapter().set_bluetooth_enabled(False)

    assert enabled is False
    assert commands == [
        "Disable-PnpDevice -InstanceId 'USB\\VID_8087&PID_0033\\5&2C2A' -Confirm:$false"
    ]


@pytest.mark.asyncio
async def test_local_bluetooth_toggle_rejects_profile_only_devices(monkeypatch):
    async def fake_json_powershell(script):
        return {
            "FriendlyName": "Bluetooth A2DP Source",
            "InstanceId": r"BTHENUM\{0000110E-0000-1000-8000-00805F9B34FB}\DEVICE",
            "Status": "OK",
        }

    monkeypatch.setattr(
        LocalWindowsDeviceAdapter,
        "_json_powershell",
        staticmethod(fake_json_powershell),
    )

    with pytest.raises(RuntimeError, match="No controllable Bluetooth radio"):
        await LocalWindowsDeviceAdapter().set_bluetooth_enabled(True)


@pytest.mark.asyncio
async def test_local_wifi_connect_and_named_disconnect_target_interface(monkeypatch):
    calls = []

    async def fake_json_powershell(script):
        return {"Name": "Wi-Fi", "Status": "Up"}

    async def fake_run(*args):
        calls.append(args)
        if args == ("netsh", "wlan", "show", "interfaces"):
            return """
Name                   : Wi-Fi
State                  : connected
SSID                   : Office
"""
        return ""

    monkeypatch.setattr(
        LocalWindowsDeviceAdapter,
        "_json_powershell",
        staticmethod(fake_json_powershell),
    )
    monkeypatch.setattr(LocalWindowsDeviceAdapter, "_run", staticmethod(fake_run))

    adapter = LocalWindowsDeviceAdapter()
    await adapter.connect_wifi_network("Office")
    await adapter.disconnect_wifi_network("Office")

    assert calls == [
        (
            "netsh",
            "wlan",
            "connect",
            "name=Office",
            "ssid=Office",
            "interface=Wi-Fi",
        ),
        ("netsh", "wlan", "show", "interfaces"),
        ("netsh", "wlan", "disconnect", "interface=Wi-Fi"),
    ]


@pytest.mark.asyncio
async def test_local_bluetooth_disconnect_targets_paired_device_by_name(monkeypatch):
    commands = []

    async def fake_json_powershell(script):
        return [
            {
                "FriendlyName": "Intel(R) Wireless Bluetooth(R)",
                "InstanceId": r"USB\VID_8087&PID_0033\5&2C2A",
                "Status": "OK",
            },
            {
                "FriendlyName": "Dylan Headphones",
                "InstanceId": r"BTHENUM\DEV_1234\7&ABC",
                "Status": "OK",
            },
        ]

    async def fake_powershell(script):
        commands.append(script)
        return ""

    monkeypatch.setattr(
        LocalWindowsDeviceAdapter,
        "_json_powershell",
        staticmethod(fake_json_powershell),
    )
    monkeypatch.setattr(
        LocalWindowsDeviceAdapter, "_powershell", staticmethod(fake_powershell)
    )

    await LocalWindowsDeviceAdapter().disconnect_bluetooth_device("headphones")

    assert commands == [
        "Disable-PnpDevice -InstanceId 'BTHENUM\\DEV_1234\\7&ABC' -Confirm:$false"
    ]


@pytest.mark.asyncio
async def test_local_bluetooth_disconnect_resolves_generic_headphones_alias(monkeypatch):
    commands = []

    async def fake_json_powershell(script):
        if "Get-PnpDevice -PresentOnly" in script:
            return [
                {
                    "FriendlyName": "Keyboard",
                    "Class": "Keyboard",
                    "InstanceId": r"HID\KEYBOARD",
                    "Status": "OK",
                },
                {
                    "FriendlyName": "Headphones (soundcore q20i Stereo)",
                    "Class": "AudioEndpoint",
                    "InstanceId": r"SWD\MMDEVAPI\{0.0.0.00000000}",
                    "Status": "OK",
                },
            ]
        if "*headphones*" in script:
            return []
        if "*soundcore q20i*" in script:
            return {
                "FriendlyName": "soundcore q20i",
                "InstanceId": r"BTHENUM\DEV_5678\9&DEF",
                "Status": "OK",
            }
        return []

    async def fake_powershell(script):
        commands.append(script)
        return ""

    monkeypatch.setattr(
        LocalWindowsDeviceAdapter,
        "_json_powershell",
        staticmethod(fake_json_powershell),
    )
    monkeypatch.setattr(
        LocalWindowsDeviceAdapter, "_powershell", staticmethod(fake_powershell)
    )

    await LocalWindowsDeviceAdapter().disconnect_bluetooth_device("headphones")

    assert commands == [
        "Disable-PnpDevice -InstanceId 'BTHENUM\\DEV_5678\\9&DEF' -Confirm:$false"
    ]


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
    scopes = skill.required_permissions(skill_call)
    assert result.status == ResultStatus.COMPLETED
    assert not destination.exists()
    assert scopes[0].capability == "filesystem.read"
    assert scopes[0].targets == (str(source.resolve()),)
    assert scopes[1].capability == "filesystem.write"
    assert scopes[1].targets == (str(destination.resolve()),)
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
async def test_file_create_text_and_reliable_rollback(tmp_path):
    target = tmp_path / "my_text_file.txt"
    adapter = FakeFiles()
    skill = FileOperationSkill(adapter)
    skill_call = call(
        "windows.file_operation",
        {
            "operation": "create_text",
            "source": str(target),
            "content": "hello",
        },
    )

    scopes = skill.required_permissions(skill_call)
    result = await skill.execute(skill_call, __import__("asyncio").Event())

    assert scopes[0].capability == "filesystem.write"
    assert scopes[0].targets == (str(target.resolve()),)
    assert skill.risk_for(skill_call) == RiskLevel.MEDIUM
    assert target.read_text(encoding="utf-8") == "hello"
    rollback = await skill.rollback(result.rollback_token)
    assert rollback.status == ResultStatus.COMPLETED
    assert not target.exists()


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
async def test_window_movement_and_resize_are_typed():
    apps = FakeApps()
    skill = ApplicationWindowSkill(apps)
    move_call = call(
        "windows.application_window",
        {
            "operation": "move_to_monitor",
            "process_name": "brave.exe",
            "monitor": 2,
        },
    )
    preview = await skill.preview(move_call)
    result = await skill.execute(move_call, __import__("asyncio").Event())
    assert "monitor 2" in preview
    assert result.data["monitor"] == 2
    assert apps.calls == [("move_to_monitor", "brave.exe", 2)]

    resize_call = call(
        "windows.application_window",
        {
            "operation": "resize",
            "process_name": "brave.exe",
            "width": 1280,
            "height": 720,
        },
    )
    await skill.execute(resize_call, __import__("asyncio").Event())
    assert apps.calls[-1] == ("resize", "brave.exe", 1280, 720)
    assert skill.required_permissions(move_call)[0].capability == "process.window"


@pytest.mark.asyncio
async def test_focused_window_minimize_and_maximize_do_not_require_process_name():
    apps = FakeApps()
    skill = ApplicationWindowSkill(apps)
    minimize_call = call(
        "windows.application_window",
        {"operation": "minimize_focused"},
    )
    maximize_call = call(
        "windows.application_window",
        {"operation": "maximize_focused"},
    )

    assert await skill.preview(minimize_call) == "Minimize focused window"
    result = await skill.execute(minimize_call, __import__("asyncio").Event())
    await skill.execute(maximize_call, __import__("asyncio").Event())

    assert result.data["process_name"] is None
    assert apps.calls == [("minimize_focused",), ("maximize_focused",)]
    scope = skill.required_permissions(maximize_call)[0]
    assert scope.capability == "process.window"
    assert scope.targets == ("focused-window",)


def test_window_movement_requires_operation_specific_arguments():
    with pytest.raises(ValueError, match="monitor"):
        WindowInput(operation="move_to_monitor", process_name="brave.exe")

    with pytest.raises(ValueError, match="process_name"):
        WindowInput(operation="maximize")


def test_assistant_window_lookup_includes_developer_python_hosts(monkeypatch):
    def fake_pids(process_name):
        return {
            "LocalAssistant.exe": {10},
            "python.exe": {20},
            "pythonw.exe": {30},
        }.get(process_name, set())

    monkeypatch.setattr(WindowsApplicationAdapter, "_pids", staticmethod(fake_pids))

    pids, title_filtered_pids = WindowsApplicationAdapter._target_pid_sets(
        "LocalAssistant.exe"
    )

    assert pids == {10}
    assert title_filtered_pids == {20, 30}


def test_assistant_developer_window_requires_local_assistant_title(monkeypatch):
    def fake_title(hwnd):
        return "Local Assistant" if hwnd == 100 else "Other Window"

    monkeypatch.setattr(
        WindowsApplicationAdapter, "_window_title", staticmethod(fake_title)
    )

    assert WindowsApplicationAdapter._window_matches_target(
        200, 10, {10}, {20}
    )
    assert WindowsApplicationAdapter._window_matches_target(
        100, 20, {10}, {20}
    )
    assert not WindowsApplicationAdapter._window_matches_target(
        101, 20, {10}, {20}
    )


@pytest.mark.asyncio
async def test_context_navigation_prefers_active_browser_then_visible_fallback():
    adapter = FakeContextNavigation(active="chrome.exe", visible="brave.exe")
    skill = ContextNavigationSkill(adapter)
    skill_call = call(
        "windows.context_navigation",
        {"target": "browser", "operation": "new_tab"},
    )

    result = await skill.execute(skill_call, __import__("asyncio").Event())

    assert result.data == {
        "target": "browser",
        "operation": "new_tab",
        "process_name": "chrome.exe",
    }
    assert adapter.focused == []
    assert adapter.hotkeys == [("ctrl", "t")]
    assert [scope.capability for scope in skill.required_permissions(skill_call)] == [
        "process.window",
        "input.control",
    ]
    assert skill.risk_for(skill_call) == RiskLevel.LOW

    adapter = FakeContextNavigation(active="notepad.exe", visible="brave.exe")
    skill = ContextNavigationSkill(adapter)
    await skill.execute(
        call(
            "windows.context_navigation",
            {"target": "browser", "operation": "back"},
        ),
        __import__("asyncio").Event(),
    )
    assert adapter.focused == ["brave.exe"]
    assert adapter.hotkeys == [("alt", "left")]


@pytest.mark.asyncio
async def test_context_navigation_requires_focused_or_visible_browser():
    skill = ContextNavigationSkill(FakeContextNavigation(active="notepad.exe"))

    with pytest.raises(RuntimeError, match="No focused or visible browser"):
        await skill.execute(
            call(
                "windows.context_navigation",
                {"target": "browser", "operation": "new_tab"},
            ),
            __import__("asyncio").Event(),
        )


@pytest.mark.asyncio
async def test_browser_navigate_opens_url_in_active_browser_address_bar():
    adapter = FakeContextNavigation(active="chrome.exe", visible="brave.exe")
    skill = BrowserNavigateSkill(adapter)
    skill_call = call(
        "windows.browser_navigate",
        {"target": "browser", "url": "https://example.com/docs"},
    )

    result = await skill.execute(skill_call, __import__("asyncio").Event())

    assert result.data == {
        "url": "https://example.com/docs",
        "process_name": "chrome.exe",
    }
    assert adapter.focused == []
    assert adapter.hotkeys == [("ctrl", "l"), ("enter",)]
    assert adapter.typed == ["https://example.com/docs"]
    assert [scope.capability for scope in skill.required_permissions(skill_call)] == [
        "process.window",
        "input.control",
        "network",
    ]
    assert skill.risk_for(skill_call) == RiskLevel.LOW


@pytest.mark.asyncio
async def test_browser_navigate_focuses_visible_browser_when_active_is_not_browser():
    adapter = FakeContextNavigation(active="notepad.exe", visible="brave.exe")
    skill = BrowserNavigateSkill(adapter)

    await skill.execute(
        call(
            "windows.browser_navigate",
            {"target": "browser", "url": "https://example.com/"},
        ),
        __import__("asyncio").Event(),
    )

    assert adapter.focused == ["brave.exe"]
    assert adapter.hotkeys == [("ctrl", "l"), ("enter",)]
    assert adapter.typed == ["https://example.com/"]


@pytest.mark.asyncio
async def test_browser_navigate_asks_when_private_and_regular_windows_exist():
    adapter = FakeContextNavigation(
        active="notepad.exe",
        visible_windows=(
            BrowserWindowInfo("brave.exe", private=False),
            BrowserWindowInfo("brave.exe", private=True),
        ),
    )
    skill = BrowserNavigateSkill(adapter)

    result = await skill.execute(
        call(
            "windows.browser_navigate",
            {"target": "browser", "url": "https://example.com/"},
        ),
        __import__("asyncio").Event(),
    )

    assert result.status == ResultStatus.DENIED
    assert result.error.code == "clarification_required"
    assert "Which browser window" in result.summary
    assert adapter.hotkeys == []


def test_installed_application_executable_resolution(tmp_path):
    install = tmp_path / "Example App"
    install.mkdir()
    app = install / "ExampleApp.exe"
    app.write_bytes(b"")
    updater = install / "Update.exe"
    updater.write_bytes(b"")

    assert LocalWindowsDeviceAdapter._display_icon_executable(
        f'"{app}",0'
    ) == app.resolve()
    assert LocalWindowsDeviceAdapter._application_executable_path(
        "Example App", None, str(install)
    ) == str(app.resolve())


def test_default_browser_command_executable_path_is_parsed():
    assert WindowsApplicationAdapter._command_executable_path(
        r'"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --single-argument %1'
    ) == r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    assert WindowsApplicationAdapter._command_executable_path(
        r"C:\Browsers\Firefox\firefox.exe -osint -url %1"
    ) == r"C:\Browsers\Firefox\firefox.exe"


def test_window_input_trims_trailing_planner_punctuation():
    skill = ApplicationWindowSkill(FakeApps())
    skill_call = call(
        "windows.application_window",
        {"operation": "close", "process_name": "roblox.exe\ufe40"},
    )
    assert skill.required_permissions(skill_call)[0].targets == (
        "robloxplayerbeta.exe",
    )


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

    toggle_call = call(
        "windows.setting",
        {"operation": "toggle", "setting": "dark_mode"},
    )
    result = await skill.execute(toggle_call, __import__("asyncio").Event())
    assert result.data["enabled"] is False
    assert skill.required_permissions(toggle_call)[0].capability == "settings.set"


@pytest.mark.asyncio
async def test_windows_device_control_covers_status_and_local_controls():
    adapter = FakeDeviceControl()
    skill = WindowsDeviceControlSkill(adapter)

    search_call = call(
        "windows.device_control",
        {"operation": "search_installed_apps", "query": "note"},
    )
    result = await skill.execute(search_call, __import__("asyncio").Event())
    assert result.data["result"][0]["name"] == "Notepad"
    assert skill.required_permissions(search_call)[0].capability == "system.apps"
    assert skill.risk_for(search_call) == RiskLevel.LOW

    volume_call = call(
        "windows.device_control",
        {"operation": "set_volume", "level": 35},
    )
    await skill.execute(volume_call, __import__("asyncio").Event())
    assert adapter.calls[-1] == ("set_volume", 35)
    assert skill.required_permissions(volume_call)[0].capability == "media.control"

    brightness_call = call(
        "windows.device_control",
        {"operation": "change_brightness", "delta": -10},
    )
    result = await skill.execute(brightness_call, __import__("asyncio").Event())
    assert result.data["result"]["level"] == 40
    assert skill.required_permissions(brightness_call)[0].capability == "system.display"

    wifi_call = call(
        "windows.device_control",
        {"operation": "toggle_wifi", "enabled": False},
    )
    result = await skill.execute(wifi_call, __import__("asyncio").Event())
    assert result.data["result"]["enabled"] is False
    assert skill.required_permissions(wifi_call)[0].capability == "system.network"

    battery_call = call("windows.device_control", {"operation": "battery_status"})
    result = await skill.execute(battery_call, __import__("asyncio").Event())
    assert result.data["result"]["percent"] == 80
    assert skill.required_permissions(battery_call)[0].capability == "device.read"

    restart_call = call("windows.device_control", {"operation": "restart_device"})
    assert skill.risk_for(restart_call) == RiskLevel.CRITICAL
    assert skill.required_permissions(restart_call)[0].capability == "system.power"


@pytest.mark.asyncio
async def test_windows_device_control_covers_remaining_checklist_commands():
    adapter = FakeDeviceControl()
    skill = WindowsDeviceControlSkill(adapter)
    event = __import__("asyncio").Event()

    for arguments, expected_call in [
        ({"operation": "change_volume", "delta": 10}, ("change_volume", 10)),
        ({"operation": "mute_audio"}, ("mute_audio", True)),
        ({"operation": "unmute_audio"}, ("mute_audio", False)),
        ({"operation": "set_brightness", "level": 70}, ("set_brightness", 70)),
        ({"operation": "toggle_bluetooth"}, ("set_bluetooth_enabled", None)),
        (
            {"operation": "connect_wifi_network", "network_name": "Office"},
            ("connect_wifi_network", "Office"),
        ),
        (
            {"operation": "disconnect_wifi_network", "network_name": "Office"},
            ("disconnect_wifi_network", "Office"),
        ),
        (
            {"operation": "connect_bluetooth_device", "device_name": "Headphones"},
            ("connect_bluetooth_device", "Headphones"),
        ),
        (
            {"operation": "disconnect_bluetooth_device", "device_name": "Headphones"},
            ("disconnect_bluetooth_device", "Headphones"),
        ),
        ({"operation": "lock_device"}, ("lock_device",)),
        ({"operation": "shutdown_device"}, ("shutdown_device",)),
        ({"operation": "storage_status", "drive": "C:"}, ("storage_status", "C:")),
        ({"operation": "performance_status"}, ("performance_status",)),
        ({"operation": "connected_devices"}, ("connected_devices", 20)),
        (
            {"operation": "disable_connected_device", "instance_id": "USB\\VID_1"},
            ("set_connected_device_enabled", "USB\\VID_1", False),
        ),
    ]:
        await skill.execute(call("windows.device_control", arguments), event)
        assert adapter.calls[-1] == expected_call

    disable_call = call(
        "windows.device_control",
        {"operation": "disable_connected_device", "instance_id": "USB\\VID_1"},
    )
    assert skill.required_permissions(disable_call)[0].capability == "device.control"
    assert skill.required_permissions(disable_call)[0].targets == ("USB\\VID_1",)
    assert skill.risk_for(disable_call) == RiskLevel.HIGH

    bluetooth_call = call(
        "windows.device_control",
        {"operation": "disconnect_bluetooth_device", "device_name": "Headphones"},
    )
    assert skill.required_permissions(bluetooth_call)[0].capability == "system.network"
    assert skill.required_permissions(bluetooth_call)[0].targets == ("Headphones",)
