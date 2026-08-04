import pytest

from assistant.router.deterministic import DeterministicRouter
from assistant.router.normalization import command_text, normalize_text, spoken_number


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


def test_result_selection_is_generic_assistant_control():
    result = DeterministicRouter().route("select item task-42")

    assert result.intent.name == "control.result_select"
    assert result.intent.arguments == {"selector": "task-42"}
    assert not result.needs_llm


def test_timer_normalizes_spoken_number():
    result = DeterministicRouter().route("set a timer for twenty five minutes")
    assert result.intent.arguments["seconds"] == 1500


def test_relative_reminder_routes_to_durable_reminder_skill():
    result = DeterministicRouter().route("remind me in twenty five minutes to check the oven")

    assert not result.needs_llm
    assert result.intent.name == "core.reminder"
    assert result.intent.arguments == {
        "title": "check the oven",
        "delay_seconds": 1500,
    }


@pytest.mark.parametrize(
    ("text", "recurrence"),
    [
        (
            "remind me every weekday at 8:30 am to review tasks",
            {
                "frequency": "weekly",
                "interval": 1,
                "local_time": "08:30:00",
                "weekdays": (0, 1, 2, 3, 4),
            },
        ),
        (
            "remind me every monday at 9 pm to take out the trash",
            {
                "frequency": "weekly",
                "interval": 1,
                "local_time": "21:00:00",
                "weekdays": (0,),
            },
        ),
        (
            "remind me every two hours to stretch",
            {"frequency": "hours", "interval": 2},
        ),
    ],
)
def test_recurring_reminder_routes_to_schedule_skill(text, recurrence):
    result = DeterministicRouter().route(text)

    assert not result.needs_llm
    assert result.intent.name == "core.schedule"
    assert result.intent.arguments["recurrence"] == recurrence


@pytest.mark.parametrize(
    "text",
    [
        "Say good morning! everyday at 8 AM",
        "say good morning every day at 8 am",
        "announce good morning at 8 am daily",
        "every day at 8 am say good morning",
    ],
)
def test_daily_message_routes_to_durable_schedule(text):
    result = DeterministicRouter().route(text)

    assert not result.needs_llm
    assert result.clarification is None
    assert result.intent.name == "core.schedule"
    assert result.intent.arguments == {
        "operation": "create",
        "name": "good morning!" if "!" in text else "good morning",
        "message": "good morning!" if "!" in text else "good morning",
        "recurrence": {
            "frequency": "daily",
            "interval": 1,
            "local_time": "08:00:00",
        },
    }


def test_daily_message_with_ambiguous_time_asks_for_am_or_pm():
    result = DeterministicRouter().route("say good morning every day at 8")

    assert result.intent is None
    assert result.clarification == ("What time should the daily message run, including AM or PM?")


def test_daily_amazon_deals_route_to_a_durable_background_web_action():
    result = DeterministicRouter().route("Show me daily tech deals at 8 am from amazon")

    assert not result.needs_llm
    assert result.clarification is None
    assert result.intent.name == "core.schedule"
    assert result.intent.arguments == {
        "operation": "create_action",
        "name": "Daily Amazon tech deals",
        "recurrence": {
            "frequency": "daily",
            "interval": 1,
            "local_time": "08:00:00",
        },
        "skill_id": "web.headless_read",
        "arguments": {
            "url": "https://www.amazon.com/s?k=daily+tech+deals",
            "max_characters": 12000,
            "max_links": 30,
            "timeout_seconds": 30,
            "wait_after_load_ms": 750,
            "extract": "tech deals",
            "max_items": 5,
        },
        "delivery_mode": "always",
    }


@pytest.mark.parametrize(
    "text",
    [
        "Show me tech deals from Amazon every day at 8 am",
        "Every day at 8 am, show me tech deals from Amazon",
        "At 8 am every day show me tech deals from Amazon",
        "Get daily tech deals from Amazon at 8 am",
        "Show me Amazon tech deals every day at 8 am",
        "Show me tech deals from Amazon at 8 am every day",
        "Find me tech deals on Amazon daily at 8 AM",
        "Send me Amazon tech deals daily at 8 AM",
        "Every morning at eight a.m., display tech deals from Amazon",
    ],
)
def test_daily_web_action_maps_word_order_and_voice_time_variations(text):
    result = DeterministicRouter().route(text)

    assert not result.needs_llm
    assert result.clarification is None
    assert result.intent.name == "core.schedule"
    assert result.intent.arguments["recurrence"]["local_time"] == "08:00:00"
    assert result.intent.arguments["skill_id"] == "web.headless_read"
    assert result.intent.arguments["arguments"]["extract"] == "tech deals"
    assert result.intent.arguments["arguments"]["url"] == (
        "https://www.amazon.com/s?k=daily+tech+deals"
    )


@pytest.mark.parametrize(
    ("text", "arguments"),
    [
        ("list schedules", {"operation": "list"}),
        ("pause reminder stretch", {"operation": "pause", "schedule": "stretch"}),
        ("resume schedule stretch", {"operation": "resume", "schedule": "stretch"}),
        ("delete schedule stretch", {"operation": "delete", "schedule": "stretch"}),
        ("run schedule stretch now", {"operation": "run_now", "schedule": "stretch"}),
        (
            "snooze reminder stretch for ten minutes",
            {"operation": "snooze", "schedule": "stretch", "delay_seconds": 600},
        ),
    ],
)
def test_schedule_management_routes(text, arguments):
    result = DeterministicRouter().route(text)

    assert not result.needs_llm
    assert result.intent.name == "core.schedule"
    assert result.intent.arguments == arguments


def test_random_choice_routes_to_typed_choose_skill():
    result = DeterministicRouter().route("pick a random item from tacos, pasta, or curry")

    assert result.intent is not None
    assert result.intent.name == "core.choose"
    assert result.intent.arguments == {
        "items": ("tacos", "pasta", "curry"),
        "strategy": "random",
    }

    between = DeterministicRouter().route("choose between tea and coffee")
    assert between.intent is not None
    assert between.intent.name == "core.choose"
    assert between.intent.arguments["items"] == ("tea", "coffee")


def test_advanced_choice_constraints_route_with_stable_history():
    result = DeterministicRouter().route(
        "Choose from red, green, or blue using least-recent selection, "
        "avoid the last choice, and remember the history as colors."
    )

    assert result.intent is not None
    assert result.intent.name == "core.choose"
    assert result.intent.arguments == {
        "items": ("red", "green", "blue"),
        "strategy": "least_recent",
        "avoid_recent": 1,
        "history_key": "colors",
    }
    assert result.needs_llm is False


def test_reported_delayed_choice_maps_directly_to_logic_schedule():
    result = DeterministicRouter().route(
        "In one minute, choose tea, coffee, or water at random and notify me "
        "with the choice. Stop after one run."
    )

    assert result.intent is not None
    assert result.intent.name == "core.schedule"
    assert result.intent.arguments["operation"] == "create_logic"
    assert result.intent.arguments["delay_seconds"] == 60
    workflow = result.intent.arguments["workflow"]
    assert workflow["action"]["kind"] == "notification"
    assert workflow["completion"]["max_runs"] == 1
    assert [item["label"] for item in workflow["selection"]["options"]] == [
        "tea",
        "coffee",
        "water",
    ]


def test_reported_conditional_calculation_maps_directly_to_logic_schedule():
    result = DeterministicRouter().route(
        "Every five minutes, calculate 6 * 7. If the result is greater than 40, "
        "notify me with the result and then stop. Maximum three runs."
    )

    assert result.intent is not None
    assert result.intent.name == "core.schedule"
    arguments = result.intent.arguments
    assert arguments["recurrence"] == {"frequency": "minutes", "interval": 5}
    workflow = arguments["workflow"]
    assert workflow["check"] == {
        "skill_id": "core.calculator",
        "arguments": {"expression": "6 * 7"},
    }
    assert workflow["when"]["operator"] == "greater_than"
    assert workflow["completion"] == {
        "stop_after_condition": True,
        "max_runs": 3,
    }


def test_reported_probability_request_maps_to_immediate_chance_skill():
    result = DeterministicRouter().route(
        "give me a 20 percent chance of a Surprise notification and an 80 percent "
        "chance of doing nothing."
    )

    assert result.intent is not None
    assert result.intent.name == "core.chance"
    assert result.intent.arguments == {
        "probability_percent": 20,
        "title": "Surprise",
        "message": "Surprise!",
    }
    assert result.needs_llm is False


def test_scheduled_probability_needs_no_external_condition():
    result = DeterministicRouter().route(
        "Every day at 9 AM, give me a 20 percent chance of a Surprise notification "
        "and an 80 percent chance of doing nothing. Stop after ten runs."
    )

    assert result.intent is not None
    assert result.intent.name == "core.schedule"
    arguments = result.intent.arguments
    assert arguments["operation"] == "create_logic"
    assert arguments["recurrence"] == {
        "frequency": "daily",
        "interval": 1,
        "local_time": "09:00:00",
    }
    workflow = arguments["workflow"]
    assert "check" not in workflow
    assert "when" not in workflow
    assert workflow["action"]["call"]["skill_id"] == "core.chance"
    assert workflow["completion"]["max_runs"] == 10


def test_stop_reminder_is_an_easy_pause_alias():
    result = DeterministicRouter().route("stop reminder stretch")

    assert result.intent is not None
    assert result.intent.name == "core.schedule"
    assert result.intent.arguments == {"operation": "pause", "schedule": "stretch"}


@pytest.mark.parametrize(
    ("text", "canonical", "application"),
    [
        ("Open up notepad", "open notepad", "notepad"),
        ("Start up calculator", "start calculator", "calculator"),
        ("Fire up paint", "open paint", "paint"),
    ],
)
def test_phrasal_launch_verbs_do_not_leak_particles_into_application_name(
    text, canonical, application
):
    assert command_text(text) == canonical

    result = DeterministicRouter().route(text)

    assert not result.needs_llm
    assert result.intent.name == "windows.launcher"
    assert result.intent.arguments == {"application": application}


def test_unknown_request_needs_local_planner():
    result = DeterministicRouter().route("organize these files for me")
    assert result.needs_llm
    assert result.intent.name == "planner.goal"
    assert result.intent.ambiguous


@pytest.mark.parametrize(
    ("text", "expected_intent", "needs_llm"),
    [
        ("help me calculate 2 plus 3", "planner.goal", True),
        ("what time is my timer", "planner.goal", True),
        ("open brave and search for vlr.gg", "windows.launcher", False),
        ("find my resume on my desktop", "windows.file_search", False),
        ("delete the resume in my desktop", "windows.file_operation", False),
        ("use my usual browser", "planner.goal", True),
        ("remember that I prefer Brave", "core.memory", False),
    ],
)
def test_route_precedence_is_characterized(text, expected_intent, needs_llm):
    result = DeterministicRouter().route(text)

    assert result.intent.name == expected_intent
    assert result.needs_llm is needs_llm


@pytest.mark.parametrize(
    ("text", "arguments"),
    [
        (
            "Remember that I prefer Brave for browsing.",
            {
                "operation": "remember",
                "type": "preference",
                "key": "i_prefer_brave_for_browsing",
                "value": "I prefer Brave for browsing",
            },
        ),
        (
            "What do you remember about me?",
            {"operation": "list"},
        ),
        (
            "Forget memory 12",
            {"operation": "forget", "memory_id": 12},
        ),
    ],
)
def test_explicit_memory_commands_route_without_planner(text, arguments):
    result = DeterministicRouter().route(text)

    assert not result.needs_llm
    assert result.intent.name == "core.memory"
    assert result.intent.arguments == arguments


@pytest.mark.parametrize(
    "text",
    [
        "Clean up my downloads.",
        "Help me prepare for my meeting.",
        "Figure out what I was working on yesterday.",
        "Make this easier to read.",
        "Use my usual browser.",
    ],
)
def test_broad_goal_requests_route_to_planner_goal(text):
    result = DeterministicRouter().route(text)

    assert result.needs_llm
    assert result.intent.name == "planner.goal"
    assert result.intent.arguments == {"text": text}
    assert result.intent.source == "classifier"
    assert result.intent.ambiguous
    assert result.intent.ambiguity_reasons == ("Open-ended input requires goal interpretation",)


@pytest.mark.parametrize(
    "text",
    [
        "I need a blank Notepad window",
        "Could you bring Calculator to the front?",
        "I want the volume a little lower",
        "Please find the latest report in my files",
    ],
)
def test_naturally_framed_requests_use_goal_interpretation(text):
    result = DeterministicRouter().route(text)

    assert result.needs_llm
    assert result.intent.name == "planner.goal"
    assert result.intent.arguments == {"text": text}
    assert result.intent.ambiguity_reasons == ("Open-ended input requires goal interpretation",)


def test_file_search_on_desktop_routes_without_local_planner():
    result = DeterministicRouter().route("Find my resume on my desktop")

    assert not result.needs_llm
    assert result.intent.name == "windows.file_search"
    assert result.intent.arguments == {
        "query": "resume",
        "root": "desktop",
        "limit": 20,
    }


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
def test_website_names_route_to_launcher_with_default_browser_fallback(text, arguments):
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
        ("open a tab", "new_tab"),
        ("open a new tab", "new_tab"),
        ("open a browser tab", "new_tab"),
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
    result = DeterministicRouter().route("Add a text file to the assistant folder in my desktop")

    assert not result.needs_llm
    assert result.intent.name == "windows.file_operation"
    assert result.intent.arguments == {
        "operation": "create_text",
        "source": "${assistant_folder}/my_text_file.txt",
    }


def test_contextual_ordinal_file_actions_route_without_planner():
    copy = DeterministicRouter().route("Copy the first one to my documents")
    assert not copy.needs_llm
    assert copy.intent.name == "windows.file_operation"
    assert copy.intent.arguments == {
        "operation": "copy",
        "source": "the first one",
        "destination": "my documents",
    }

    copy_and_paste = DeterministicRouter().route(
        "Copy and paste the first result into my documents folder"
    )
    assert not copy_and_paste.needs_llm
    assert copy_and_paste.intent.name == "windows.file_operation"
    assert copy_and_paste.intent.arguments == {
        "operation": "copy",
        "source": "the first result",
        "destination": "my documents folder",
    }

    opened = DeterministicRouter().route("Open the second one")
    assert not opened.needs_llm
    assert opened.intent.name == "windows.file_operation"
    assert opened.intent.arguments == {
        "operation": "open",
        "source": "the second one",
    }


@pytest.mark.parametrize(
    ("text", "url"),
    [
        (
            "Open brave and go to vlr.gg/matches?status=upcoming",
            "https://vlr.gg/matches?status=upcoming",
        ),
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
def test_switch_window_monitor_movement_routes_without_local_planner(text, process_name, monitor):
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
            "Whats my cpu and memory usage?",
            {"operation": "performance_status"},
        ),
        (
            "How much CPU and memory am I using?",
            {"operation": "performance_status"},
        ),
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
    assert result.intent.name == "planner.goal"


@pytest.mark.parametrize(
    "text",
    [
        "How do you work?",
        "Explain this skill.",
        "How are requests handled by the assistant?",
    ],
)
def test_application_knowledge_questions_route_to_open_ended_planner(text):
    result = DeterministicRouter().route(text)

    assert result.needs_llm
    assert result.intent.name == "planner.goal"


def test_save_playbook_route_preserves_instructions_and_registered_skill_ids():
    result = DeterministicRouter().route(
        "Save a playbook called Release Check using skills core.datetime, core.tasks: "
        "First inspect the release notes. Then create a final checklist."
    )

    assert not result.needs_llm
    assert result.intent.name == "core.playbook"
    assert result.intent.arguments == {
        "operation": "save",
        "name": "Release Check",
        "instructions": ("First inspect the release notes. Then create a final checklist."),
        "skill_ids": ("core.datetime", "core.tasks"),
    }


def test_save_playbook_route_accepts_spoken_with_instructions_form():
    result = DeterministicRouter().route(
        "Create a playbook named Morning Review with instructions "
        "First list my tasks, then summarize the highest priority items"
    )

    assert result.intent.name == "core.playbook"
    assert result.intent.arguments == {
        "operation": "save",
        "name": "Morning Review",
        "instructions": ("First list my tasks, then summarize the highest priority items"),
    }

    alternate = DeterministicRouter().route(
        "Save these instructions as a playbook called Morning Review: "
        "First list my tasks, then summarize the highest priority items"
    )
    assert alternate.intent.name == "core.playbook"
    assert alternate.intent.arguments["name"] == "Morning Review"


@pytest.mark.parametrize(
    ("text", "arguments"),
    [
        ("list playbooks", {"operation": "list"}),
        ("show playbook release-check", {"operation": "show", "name": "release-check"}),
        ("recall playbook Release Check", {"operation": "show", "name": "release check"}),
        (
            "disable playbook release-check",
            {"operation": "disable", "name": "release-check"},
        ),
        (
            "enable playbook release-check",
            {"operation": "enable", "name": "release-check"},
        ),
        (
            "delete playbook release-check",
            {"operation": "delete", "name": "release-check"},
        ),
    ],
)
def test_playbook_management_routes(text, arguments):
    result = DeterministicRouter().route(text)

    assert not result.needs_llm
    assert result.intent.name == "core.playbook"
    assert result.intent.arguments == arguments


def test_use_playbook_routes_to_open_ended_planner_goal():
    result = DeterministicRouter().route("Use playbook release-check to prepare version 2.0")

    assert result.needs_llm
    assert result.intent.name == "planner.goal"


def test_normalization_and_numbers():
    assert normalize_text("Hello comma WORLD") == "hello, world"
    assert spoken_number("one hundred and five") == 105
