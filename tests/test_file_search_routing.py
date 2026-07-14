import pytest

from assistant.router.deterministic import DeterministicRouter


@pytest.mark.parametrize(
    ("text", "query", "root"),
    [
        ("Look in my desktop for a file named resume", "resume", "desktop"),
        ("Look in Desktop for resume", "resume", "desktop"),
        ("Search my desktop for a file called resume", "resume", "desktop"),
        ("Look for a file named resume on my desktop", "resume", "desktop"),
        ("Locate my resume in Documents", "resume", "documents"),
        (
            "Find my resume in the assistant folder on my desktop",
            "resume",
            "desktop/assistant",
        ),
        (
            "Search for budget in the Finance folder inside Documents",
            "budget",
            "documents/finance",
        ),
    ],
)
def test_named_file_search_phrasings_route_without_local_planner(text, query, root):
    result = DeterministicRouter().route(text)

    assert not result.needs_llm
    assert result.intent.name == "windows.file_search"
    assert result.intent.arguments == {"query": query, "root": root, "limit": 20}


def test_generic_search_does_not_route_as_file_search():
    result = DeterministicRouter().route("Search Brave for local restaurants")

    assert result.intent.name != "windows.file_search"


def test_conceptual_time_scope_uses_goal_interpretation_not_filesystem_path():
    result = DeterministicRouter().route("Find what I was working on yesterday")

    assert result.needs_llm
    assert result.intent.name == "planner.goal"


@pytest.mark.parametrize(
    "text",
    [
        "Open my browser",
        "Launch the default browser",
        "Start my usual browser",
    ],
)
def test_relational_browser_wording_uses_browser_capability(text):
    result = DeterministicRouter().route(text)

    assert not result.needs_llm
    assert result.intent.name == "windows.launcher"
    assert result.intent.arguments == {"application": "browser"}
