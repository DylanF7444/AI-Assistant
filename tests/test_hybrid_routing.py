import pytest

from assistant.context.snapshot import ContextSnapshot
from assistant.language.planner import LocalPlanner, SkillRetriever
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.builtin import TimerSkill
from assistant.skills.network import WebSearchSkill


@pytest.mark.parametrize(
    ("text", "skill_id", "arguments"),
    [
        ("start a five minute timer", "core.timer", {"seconds": 300, "label": "Timer"}),
        ("timer for five minutes", "core.timer", {"seconds": 300, "label": "Timer"}),
        (
            "remind me to check the oven in twenty minutes",
            "core.reminder",
            {"title": "check the oven", "delay_seconds": 1200},
        ),
        (
            "in twenty minutes remind me to check the oven",
            "core.reminder",
            {"title": "check the oven", "delay_seconds": 1200},
        ),
        (
            "every monday at 9 am remind me to submit the report",
            "core.schedule",
            {
                "operation": "create",
                "name": "submit the report",
                "message": "submit the report",
                "recurrence": {
                    "frequency": "weekly",
                    "interval": 1,
                    "local_time": "09:00:00",
                    "weekdays": (0,),
                },
            },
        ),
        ("what time is it", "core.datetime", {"query": "time"}),
        ("today's date", "core.datetime", {"query": "date"}),
        ("what's 2 plus 2", "core.calculator", {"expression": "2 + 2"}),
        (
            "how many feet are in two meters",
            "core.unit_conversion",
            {"value": 2, "from_unit": "meters", "to_unit": "feet"},
        ),
        ("new task buy milk", "core.tasks", {"operation": "add", "title": "buy milk"}),
        (
            "add buy milk to my tasks",
            "core.tasks",
            {"operation": "add", "title": "buy milk"},
        ),
        ("complete task 3", "core.tasks", {"operation": "complete", "item_id": 3}),
        ("delete task 3", "core.tasks", {"operation": "delete", "item_id": 3}),
        ("show my task list", "core.tasks", {"operation": "list"}),
        ("delete note 3", "core.notes", {"operation": "delete", "item_id": 3}),
        (
            "find notes about budget",
            "core.notes",
            {"operation": "search", "query": "budget"},
        ),
        (
            "look up pizza near me",
            "web.search",
            {"query": "pizza near me", "count": 5},
        ),
        (
            "look up music theory",
            "web.search",
            {"query": "music theory", "count": 5},
        ),
        (
            "browse to example.com",
            "windows.browser_navigate",
            {"url": "https://example.com"},
        ),
        (
            "lock my computer",
            "windows.device_control",
            {"operation": "lock_device"},
        ),
        (
            "turn off my computer",
            "windows.device_control",
            {"operation": "shutdown_device"},
        ),
        (
            "how much battery do i have left",
            "windows.device_control",
            {"operation": "battery_status"},
        ),
        (
            "show free space on c",
            "windows.device_control",
            {"operation": "storage_status", "drive": "c"},
        ),
        (
            "turn the brightness up",
            "windows.device_control",
            {"operation": "change_brightness", "delta": 10},
        ),
        (
            "connect to wifi named home",
            "windows.device_control",
            {"operation": "connect_wifi_network", "network_name": "home"},
        ),
        ("next track", "windows.media", {"action": "next"}),
        ("previous track", "windows.media", {"action": "previous"}),
        ("stop music", "windows.media", {"action": "stop"}),
        (
            "turn on dark mode",
            "windows.setting",
            {"operation": "set", "setting": "dark_mode", "enabled": True},
        ),
        (
            "use light mode",
            "windows.setting",
            {"operation": "set", "setting": "dark_mode", "enabled": False},
        ),
        (
            "search downloads for resume",
            "windows.file_search",
            {"query": "resume", "root": "downloads", "limit": 20},
        ),
        (
            "open resume.pdf",
            "windows.file_operation",
            {"operation": "open", "source": "resume.pdf"},
        ),
        (
            "trash temp.txt",
            "windows.file_operation",
            {"operation": "delete", "source": "temp.txt"},
        ),
        (
            "make a file called notes.txt in documents",
            "windows.file_operation",
            {"operation": "create_text", "source": "documents/notes.txt"},
        ),
        (
            "resize the chrome window to 1280 x 720",
            "windows.application_window",
            {
                "operation": "resize",
                "process_name": "chrome.exe",
                "width": 1280,
                "height": 720,
            },
        ),
        (
            "close google chrome",
            "windows.application_window",
            {"operation": "close", "process_name": "chrome.exe"},
        ),
        ("run notepad", "windows.launcher", {"application": "notepad"}),
        (
            "kill process 1234",
            "windows.process",
            {"operation": "terminate", "pid": 1234},
        ),
        (
            "show running processes",
            "windows.process",
            {"operation": "list", "name_filter": None},
        ),
        (
            "notify me: hello",
            "windows.notification",
            {"title": "Assistant", "message": "hello"},
        ),
        ("cancel that", "control.cancel", {}),
        (
            "remember my favorite color is blue",
            "core.memory",
            {
                "operation": "remember",
                "type": "preference",
                "key": "my_favorite_color_is_blue",
                "value": "my favorite color is blue",
            },
        ),
        ("show memories", "core.memory", {"operation": "list"}),
        (
            "forget memory number 3",
            "core.memory",
            {"operation": "forget", "memory_id": 3},
        ),
    ],
)
def test_broader_high_confidence_phrases_stay_deterministic(
    text,
    skill_id,
    arguments,
):
    result = DeterministicRouter().route(text)

    assert result.intent is not None
    assert result.intent.name == skill_id
    assert result.intent.arguments == arguments
    assert not result.needs_llm


@pytest.mark.parametrize(
    "text",
    [
        "search for pizza near me",
        "remember buy milk",
        "mute it",
        "run unit tests",
        "close the door",
        "what is the capital of France",
        "what time is my timer",
        "copy this to the clipboard",
    ],
)
def test_ambiguous_or_open_ended_phrases_use_structured_ai_fallback(text):
    result = DeterministicRouter().route(text)

    assert result.intent is not None
    assert result.intent.name == "planner.goal"
    assert result.needs_llm


def test_file_and_web_targets_are_disambiguated_by_typed_evidence():
    file_result = DeterministicRouter().route("open resume.pdf")
    web_result = DeterministicRouter().route("open example.com")

    assert file_result.intent.name == "windows.file_operation"
    assert web_result.intent.name == "windows.browser_navigate"


def test_domain_specific_item_deletion_precedes_generic_file_deletion():
    task = DeterministicRouter().route("delete task 3")
    note = DeterministicRouter().route("delete note 3")

    assert task.intent.name == "core.tasks"
    assert note.intent.name == "core.notes"


def test_registered_multiword_application_can_be_controlled_deterministically():
    router = DeterministicRouter(application_names=("visual studio code",))

    result = router.route("close visual studio code")

    assert result.intent.name == "windows.application_window"
    assert result.intent.arguments == {
        "operation": "close",
        "process_name": "visual studio code.exe",
    }


def test_ai_fallback_candidates_come_from_live_skill_manifests():
    web_search = WebSearchSkill(None, key=lambda provider: None).manifest
    planner = LocalPlanner(
        _LowConfidenceProvider(),
        SkillRetriever((web_search,)),
    )

    candidates = planner.candidate_skills("search for pizza near me", ())

    assert candidates == (web_search,)


class _LowConfidenceProvider:
    async def structured_completion(self, prompt, schema, timeout_seconds):
        return {
            "interpretation": {
                "user_goal": "Start a timer with an uncertain duration",
                "request_type": "local_action",
                "confidence": 0.45,
            },
            "disposition": "skill",
            "skill_id": "core.timer",
            "arguments": {"seconds": 300, "label": "Timer"},
            "answer": None,
            "confidence": 0.45,
            "clarification": None,
        }


@pytest.mark.asyncio
async def test_low_confidence_ai_action_asks_for_clarification():
    planner = LocalPlanner(
        _LowConfidenceProvider(),
        SkillRetriever((TimerSkill.manifest,)),
    )
    text = "set some kind of timer"

    decision = await planner.plan_grounded(
        text,
        context=ContextSnapshot(request_text=text, session_id="s1"),
        retrieved=(),
        candidate_skills=(TimerSkill.manifest,),
    )

    assert decision.intent is None
    assert decision.clarification is not None
    assert "not confident" in decision.clarification
