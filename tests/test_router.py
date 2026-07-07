import pytest

from assistant.router.deterministic import DeterministicRouter
from assistant.router.normalization import normalize_text, spoken_number


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("cancel", "control.cancel"),
        ("undo", "control.undo"),
        ("undo last command", "control.undo"),
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
        ("list my registered apps", "windows.registered_applications"),
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


def test_registered_applications_route_lists_launcher_registrations():
    result = DeterministicRouter().route("What are my registered applications?")

    assert not result.needs_llm
    assert result.intent.name == "windows.registered_applications"
    assert result.intent.arguments == {}


def test_application_window_command_appends_exe_without_local_planner():
    result = DeterministicRouter().route("Close roblox")
    assert not result.needs_llm
    assert result.intent.name == "windows.application_window"
    assert result.intent.arguments == {
        "operation": "close",
        "process_name": "RobloxPlayerBeta.exe",
    }


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("minimize", "minimize_focused"),
        ("maximize", "maximize_focused"),
        ("minimize this window", "minimize_focused"),
        ("maximize the current window", "maximize_focused"),
        ("minimize focused window", "minimize_focused"),
        ("maximize foreground window", "maximize_focused"),
    ],
)
def test_focused_window_commands_route_without_process_name(text, operation):
    result = DeterministicRouter().route(text)
    assert not result.needs_llm
    assert result.intent.name == "windows.application_window"
    assert result.intent.arguments == {"operation": operation}


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
        (
            "open youtube",
            {"application": "youtube", "fallback_url": "https://www.youtube.com"},
        ),
        (
            "open chatgpt",
            {"application": "chatgpt", "fallback_url": "https://chatgpt.com"},
        ),
        (
            "open stack overflow",
            {
                "application": "stack overflow",
                "fallback_url": "https://stackoverflow.com",
            },
        ),
        (
            "open vlr.gg",
            {"application": "vlr.gg", "fallback_url": "https://vlr.gg"},
        ),
    ],
)
def test_website_names_route_to_launcher_with_default_browser_fallback(
    text, arguments
):
    result = DeterministicRouter().route(text)
    assert not result.needs_llm
    assert result.intent.name == "windows.browser_navigate"
    assert result.intent.arguments == {"url": arguments["fallback_url"]}


def test_registered_website_name_routes_to_existing_browser_navigation():
    router = DeterministicRouter(application_names=lambda: {"youtube": "path"})
    result = router.route("open youtube")
    assert result.intent.name == "windows.browser_navigate"
    assert result.intent.arguments == {"url": "https://www.youtube.com"}


def test_open_https_url_routes_to_existing_browser_navigation():
    result = DeterministicRouter().route("open https://example.com/docs")

    assert not result.needs_llm
    assert result.intent.name == "windows.browser_navigate"
    assert result.intent.arguments == {"url": "https://example.com/docs"}


@pytest.mark.parametrize(
    ("text", "arguments"),
    [
        ("open a brave tab", {"application": "brave"}),
        ("open a chrome browser tab", {"application": "chrome"}),
        ("open a firefox window", {"application": "firefox"}),
        ("open a tab", {"application": "browser"}),
        ("open a new tab", {"application": "browser"}),
        ("open a browser tab", {"application": "browser"}),
        ("open a private brave tab", {"application": "brave", "private": True}),
        ("open an incognito chrome tab", {"application": "chrome", "private": True}),
        ("open an inprivate edge window", {"application": "edge", "private": True}),
        ("open a private tab in firefox", {"application": "firefox", "private": True}),
        ("open opera private", {"application": "opera", "private": True}),
        ("open an opera gx private window", {"application": "opera gx", "private": True}),
        ("open private tab in vivaldi", {"application": "vivaldi", "private": True}),
        ("open chromium incognito", {"application": "chromium", "private": True}),
        ("open private tab", {"application": "browser", "private": True}),
    ],
)
def test_browser_tab_wording_routes_to_launcher_application(text, arguments):
    result = DeterministicRouter().route(text)
    assert not result.needs_llm
    assert result.intent.name == "windows.launcher"
    assert result.intent.arguments == arguments


@pytest.mark.parametrize(
    ("text", "arguments"),
    [
        (
            "open a private brave tab with vlr.gg",
            {"application": "brave", "private": True, "url": "https://vlr.gg"},
        ),
        (
            "open private tab in brave with https://example.com/docs",
            {
                "application": "brave",
                "private": True,
                "url": "https://example.com/docs",
            },
        ),
        (
            "open an incognito chrome tab to youtube",
            {
                "application": "chrome",
                "private": True,
                "url": "https://www.youtube.com",
            },
        ),
        (
            "open youtube with a private window open for brave",
            {
                "application": "brave",
                "private": True,
                "url": "https://www.youtube.com",
            },
        ),
        (
            "open youtube in a private window for brave",
            {
                "application": "brave",
                "private": True,
                "url": "https://www.youtube.com",
            },
        ),
        (
            "open youtube in private brave window",
            {
                "application": "brave",
                "private": True,
                "url": "https://www.youtube.com",
            },
        ),
        (
            "open vivaldi tab with docs.python.org",
            {"application": "vivaldi", "url": "https://docs.python.org"},
        ),
        (
            "open private tab with vlr.gg",
            {"application": "browser", "private": True, "url": "https://vlr.gg"},
        ),
        (
            "open tab with youtube",
            {"application": "browser", "url": "https://www.youtube.com"},
        ),
    ],
)
def test_browser_tab_wording_with_website_routes_to_launcher(text, arguments):
    result = DeterministicRouter().route(text)
    assert not result.needs_llm
    assert result.intent.name == "windows.launcher"
    assert result.intent.arguments == arguments


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("new tab", "new_tab"),
        ("open a new browser window", "new_window"),
        ("close this tab", "close_tab"),
        ("reopen closed tab", "reopen_closed_tab"),
        ("next tab", "next_tab"),
        ("previous browser tab", "previous_tab"),
        ("go back", "back"),
        ("go forward", "forward"),
        ("refresh the page", "reload"),
        ("focus the address bar", "focus_address_bar"),
    ],
)
def test_context_navigation_routes_to_browser_context(text, operation):
    result = DeterministicRouter().route(text)
    assert not result.needs_llm
    assert result.intent.name == "windows.context_navigation"
    assert result.intent.arguments == {"target": "browser", "operation": operation}


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


def test_create_text_file_in_assistant_folder_routes_without_planner():
    result = DeterministicRouter().route(
        "Add a text file to the assistant folder in my desktop"
    )

    assert not result.needs_llm
    assert result.intent.name == "windows.file_operation"
    assert result.intent.arguments == {
        "operation": "create_text",
        "source": "${assistant_folder}/my_text_file.txt",
    }


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


def test_assistant_window_routes_to_packaged_process_name():
    result = DeterministicRouter().route("Switch assistant to my first monitor")
    assert not result.needs_llm
    assert result.intent.name == "windows.application_window"
    assert result.intent.arguments == {
        "operation": "move_to_monitor",
        "process_name": "LocalAssistant.exe",
        "monitor": 1,
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
        ("set my volume to 80", {"operation": "set_volume", "level": 80}),
        ("set my device volume to 80", {"operation": "set_volume", "level": 80}),
        ("increase volume by ten percent", {"operation": "change_volume", "delta": 10}),
        ("turn down the computer audio by 5 percent", {"operation": "change_volume", "delta": -5}),
        ("mute my audio", {"operation": "mute_audio"}),
        ("unmute audio", {"operation": "unmute_audio"}),
        ("unmute my audio", {"operation": "unmute_audio"}),
        ("set screen brightness to 60 percent", {"operation": "set_brightness", "level": 60}),
        (
            "decrease brightness by 15 percent",
            {"operation": "change_brightness", "delta": -15},
        ),
        ("turn off wifi", {"operation": "toggle_wifi", "enabled": False}),
        ("turn off my wifi", {"operation": "toggle_wifi", "enabled": False}),
        (
            "connect to wifi Office",
            {"operation": "connect_wifi_network", "network_name": "office"},
        ),
        (
            "disconnect from wifi Office",
            {"operation": "disconnect_wifi_network", "network_name": "office"},
        ),
        ("disconnect wifi", {"operation": "disconnect_wifi_network"}),
        ("turn off my bluetooth", {"operation": "toggle_bluetooth", "enabled": False}),
        ("toggle bluetooth", {"operation": "toggle_bluetooth", "enabled": None}),
        (
            "connect bluetooth headphones",
            {"operation": "connect_bluetooth_device", "device_name": "headphones"},
        ),
        (
            "disconnect from bluetooth headphones",
            {
                "operation": "disconnect_bluetooth_device",
                "device_name": "headphones",
            },
        ),
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
