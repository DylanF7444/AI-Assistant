import pytest

from assistant.router.deterministic import DeterministicRouter
from assistant.router.normalization import normalize_text, spoken_number


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("cancel", "control.cancel"),
        ("help", "core.help"),
        ("what time", "core.datetime"),
        ("what is the date", "core.datetime"),
        ("calculate 2 plus 3", "core.calculator"),
        ("play music", "windows.media"),
        ("volume up", "windows.media"),
        ("show battery status", "windows.device_control"),
        ("Whats my battery?", "windows.device_control"),
        ("show storage status", "windows.device_control"),
        ("show memory and cpu usage", "windows.device_control"),
        ("show connected devices", "windows.device_control"),
        ("lock the device", "windows.device_control"),
        ("terminate process 123", "windows.process"),
        ("run macro morning", "core.macro"),
        ("forget this session", "control.forget"),
        ("correct that to calculate 3 plus 4", "control.correct"),
        ("help!", "core.help"),
        ("what time?", "core.datetime"),
        ("calculate 2 plus 3.", "core.calculator"),
        (
            "How do I register an application so you can open it for me",
            "conversation.answer",
        ),
    ],
)
def test_deterministic_routes(text, name):
    result = DeterministicRouter().route(text)
    assert result.intent.name == name
    assert not result.needs_llm


def test_timer_normalizes_spoken_number():
    result = DeterministicRouter().route("set a timer for twenty five minutes")
    assert result.intent.arguments["seconds"] == 1500


def test_unknown_request_needs_local_planner():
    result = DeterministicRouter().route("organize these files for me")
    assert result.needs_llm
    assert result.intent.ambiguous


def test_application_registration_help_does_not_need_local_planner():
    result = DeterministicRouter().route(
        "How do I register an application so you can open it for me"
    )
    assert not result.needs_llm
    assert "Applications tab" in result.intent.arguments["text"]
    assert "open <name>" in result.intent.arguments["text"]


def test_application_window_command_appends_exe_without_local_planner():
    result = DeterministicRouter().route("Close roblox")
    assert not result.needs_llm
    assert result.intent.name == "windows.application_window"
    assert result.intent.arguments == {
        "operation": "close",
        "process_name": "RobloxPlayerBeta.exe",
    }


def test_launch_command_splits_follow_up_action_from_application_name():
    result = DeterministicRouter().route("Open brave and search for vlr.gg")
    assert not result.needs_llm
    assert result.intent.name == "windows.launcher"
    assert result.intent.arguments == {
        "application": "brave",
        "url": "https://search.brave.com/search?q=vlr.gg",
    }
    assert result.intents == ()


def test_launch_command_uses_registered_app_prefix_before_follow_up():
    router = DeterministicRouter(application_names=lambda: {"brave browser": "path"})
    result = router.route("Open Brave Browser search for vlr.gg")
    assert not result.needs_llm
    assert result.intent.name == "windows.launcher"
    assert result.intent.arguments == {
        "application": "brave browser",
        "url": "https://search.brave.com/search?q=vlr.gg",
    }
    assert result.intents == ()


@pytest.mark.parametrize(
    ("text", "arguments"),
    [
        ("open a brave tab", {"application": "brave"}),
        ("open a chrome browser tab", {"application": "chrome"}),
        ("open a firefox window", {"application": "firefox"}),
        ("open a private brave tab", {"application": "brave", "private": True}),
        ("open an incognito chrome tab", {"application": "chrome", "private": True}),
        ("open an inprivate edge window", {"application": "edge", "private": True}),
        ("open a browser tab", {"application": "browser"}),
    ],
)
def test_browser_tab_wording_routes_to_launcher_application(text, arguments):
    result = DeterministicRouter().route(text)
    assert not result.needs_llm
    assert result.intent.name == "windows.launcher"
    assert result.intent.arguments == arguments


@pytest.mark.parametrize(
    "text",
    [
        "open a notepad file of this assistant's features",
        "open a blank notepad document",
        "start a text file in notepad about today's tasks",
    ],
)
def test_notepad_file_wording_routes_to_notepad_launcher(text):
    result = DeterministicRouter().route(text)
    assert not result.needs_llm
    assert result.intent.name == "windows.launcher"
    assert result.intent.arguments == {"application": "notepad"}


@pytest.mark.parametrize(
    ("text", "url"),
    [
        ("Open brave and go to vlr.gg/matches?status=upcoming", "https://vlr.gg/matches?status=upcoming"),
        ("Open brave vlr.gg", "https://vlr.gg"),
        ("Open brave and open https://example.com/docs", "https://example.com/docs"),
        ("Open brave and go to vlr dot gg", "https://vlr.gg"),
        ("Open brave and go to vlr dot gg slash matches", "https://vlr.gg/matches"),
        ("Open twitch.tv on brave", "https://twitch.tv"),
    ],
)
def test_launch_browser_follow_up_accepts_websites(text, url):
    result = DeterministicRouter().route(text)
    assert not result.needs_llm
    assert result.intent.name == "windows.launcher"
    assert result.intent.arguments == {"application": "brave", "url": url}
    assert result.intents == ()


def test_launch_browser_strips_monitor_placement_from_application_name():
    result = DeterministicRouter().route("Open brave on my first monitor")
    assert not result.needs_llm
    assert result.intent.name == "windows.launcher"
    assert result.intent.arguments == {"application": "brave"}
    assert [intent.name for intent in result.intents] == [
        "windows.launcher",
        "windows.application_window",
    ]
    assert result.intents[1].arguments == {
        "operation": "move_to_monitor",
        "process_name": "brave.exe",
        "monitor": 1,
    }


def test_window_monitor_movement_routes_before_file_move():
    result = DeterministicRouter().route("Move brave to monitor 2")
    assert not result.needs_llm
    assert result.intent.name == "windows.application_window"
    assert result.intent.arguments == {
        "operation": "move_to_monitor",
        "process_name": "brave.exe",
        "monitor": 2,
    }


@pytest.mark.parametrize(
    ("text", "process_name", "monitor"),
    [
        ("Switch notepad to my first monitor", "notepad.exe", 1),
        ("Switch brave window to the second screen", "brave.exe", 2),
        ("Switch chrome on display 3", "chrome.exe", 3),
    ],
)
def test_switch_window_monitor_movement_routes_without_local_planner(
    text, process_name, monitor
):
    result = DeterministicRouter().route(text)
    assert not result.needs_llm
    assert result.intent.name == "windows.application_window"
    assert result.intent.arguments == {
        "operation": "move_to_monitor",
        "process_name": process_name,
        "monitor": monitor,
    }


def test_window_coordinate_move_and_resize_route():
    move = DeterministicRouter().route("Move brave to x 100 y 200")
    assert move.intent.name == "windows.application_window"
    assert move.intent.arguments == {
        "operation": "move",
        "process_name": "brave.exe",
        "x": 100,
        "y": 200,
    }

    resize = DeterministicRouter().route("Resize brave to 1280 by 720")
    assert resize.intent.name == "windows.application_window"
    assert resize.intent.arguments == {
        "operation": "resize",
        "process_name": "brave.exe",
        "width": 1280,
        "height": 720,
    }


@pytest.mark.parametrize(
    ("text", "arguments"),
    [
        (
            "search installed apps for notepad",
            {"operation": "search_installed_apps", "query": "notepad"},
        ),
        ("set system volume to 35 percent", {"operation": "set_volume", "level": 35}),
        ("increase volume by ten percent", {"operation": "change_volume", "delta": 10}),
        ("unmute audio", {"operation": "unmute_audio"}),
        ("set screen brightness to 60 percent", {"operation": "set_brightness", "level": 60}),
        (
            "decrease brightness by 15 percent",
            {"operation": "change_brightness", "delta": -15},
        ),
        ("turn off wifi", {"operation": "toggle_wifi", "enabled": False}),
        ("turn off my wifi", {"operation": "toggle_wifi", "enabled": False}),
        ("toggle bluetooth", {"operation": "toggle_bluetooth", "enabled": None}),
        ("restart the computer", {"operation": "restart_device"}),
        ("shutdown computer", {"operation": "shutdown_device"}),
        ("what's my battery level", {"operation": "battery_status"}),
        ("battery status", {"operation": "battery_status"}),
        (
            "disable connected device USB\\VID_1",
            {"operation": "disable_connected_device", "instance_id": "usb\\vid_1"},
        ),
    ],
)
def test_device_control_routes(text, arguments):
    result = DeterministicRouter().route(text)
    assert not result.needs_llm
    assert result.intent.name == "windows.device_control"
    assert result.intent.arguments == arguments


def test_light_and_dark_mode_routes():
    dark = DeterministicRouter().route("toggle dark mode")
    assert dark.intent.name == "windows.setting"
    assert dark.intent.arguments == {"operation": "toggle", "setting": "dark_mode"}

    light = DeterministicRouter().route("turn light mode on")
    assert light.intent.name == "windows.setting"
    assert light.intent.arguments == {
        "operation": "set",
        "setting": "dark_mode",
        "enabled": False,
    }


def test_knowledge_question_is_not_misrouted_to_calculator():
    result = DeterministicRouter().route("What is the capital of France?")
    assert result.needs_llm
    assert result.intent.name == "conversation.question"


def test_normalization_and_numbers():
    assert normalize_text("Hello comma WORLD") == "hello, world"
    assert spoken_number("one hundred and five") == 105
