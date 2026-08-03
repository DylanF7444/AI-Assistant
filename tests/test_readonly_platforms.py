from __future__ import annotations

import asyncio
import io
import json

import pytest

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.contracts import PermissionContext, RiskLevel, SkillCall
from assistant.integrations.github import GitHubConnectionService
from assistant.integrations.notion import NotionConnectionService
from assistant.integrations.slack import SlackConnectionService
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.github import GitHubInput, GitHubSkill
from assistant.skills.notion import NotionInput, NotionSkill
from assistant.skills.slack import SlackInput, SlackSkill


class MemoryVault:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class RecordingAdapter:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    async def request(self, method, path, *, query=None, body=None, safe_to_retry=None):
        self.requests.append(
            {
                "method": method,
                "path": path,
                "query": query,
                "body": body,
                "safe_to_retry": safe_to_retry,
            }
        )
        return self.responses.pop(0) if self.responses else {}

    async def health(self):
        return {"healthy": True}


def call(skill_id, arguments):
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


@pytest.mark.parametrize(
    ("text", "skill_id", "operation"),
    [
        ("find my Slack for launch", "integration.slack", "search_messages"),
        ("list my Slack channels", "integration.slack", "list_conversations"),
        ("find my Notion for planning", "integration.notion", "search"),
        ("find my GitHub for assistant", "integration.github", "search_repositories"),
    ],
)
def test_readonly_platform_requests_route_without_the_llm(text, skill_id, operation):
    route = DeterministicRouter().route(text)
    assert route.needs_llm is False
    assert route.intent.name == skill_id
    assert route.intent.arguments["operation"] == operation


def test_readonly_platform_inputs_require_explicit_targets():
    with pytest.raises(ValueError, match="requires query"):
        SlackInput(operation="search_messages")
    with pytest.raises(ValueError, match="requires resource_id"):
        NotionInput(operation="get_page")
    with pytest.raises(ValueError, match="requires owner and repo"):
        GitHubInput(operation="get_repository")


def test_readonly_platform_permissions_are_exact_and_low_risk():
    slack = SlackSkill(RecordingAdapter())
    notion = NotionSkill(RecordingAdapter())
    github = GitHubSkill(RecordingAdapter())
    slack_call = call("integration.slack", {"operation": "read_history", "channel_id": "C123"})
    notion_call = call("integration.notion", {"operation": "get_page", "resource_id": "page-1"})
    github_call = call(
        "integration.github",
        {"operation": "get_issue", "owner": "openai", "repo": "assistant", "issue_number": 7},
    )

    assert slack.required_permissions(slack_call)[0].targets == ("slack:channel:C123",)
    assert notion.required_permissions(notion_call)[0].targets == ("notion:get_page:page-1",)
    assert github.required_permissions(github_call)[0].targets == (
        "github:issue:openai/assistant:7",
    )
    assert slack.risk_for(slack_call) == RiskLevel.LOW
    assert notion.risk_for(notion_call) == RiskLevel.LOW
    assert github.risk_for(github_call) == RiskLevel.LOW


@pytest.mark.asyncio
async def test_slack_search_and_history_return_bounded_selectable_items():
    adapter = RecordingAdapter(
        [
            {
                "messages": {"matches": [{"iid": "m-1", "text": "Launch plan", "ts": "1"}]},
                "response_metadata": {"next_cursor": "next"},
            },
            {"messages": [{"ts": "2", "text": "Private message"}]},
        ]
    )
    skill = SlackSkill(adapter)
    result = await skill.execute(
        call("integration.slack", {"operation": "search_messages", "query": "launch"}),
        asyncio.Event(),
    )
    await skill.execute(
        call("integration.slack", {"operation": "read_history", "channel_id": "C123"}),
        asyncio.Event(),
    )

    assert adapter.requests[0]["path"] == "/search.messages"
    assert adapter.requests[0]["query"]["query"] == "launch"
    assert result.data["next_page_token"] == "next"
    assert result.items[0].sensitive is True
    assert adapter.requests[1]["query"]["channel"] == "C123"


@pytest.mark.asyncio
async def test_notion_search_uses_current_version_and_id_paths():
    adapter = RecordingAdapter(
        [
            {
                "results": [{"id": "page-1", "object": "page", "url": "https://notion.so/page"}],
                "next_cursor": "cursor",
            },
            {"object": "page", "id": "page-1"},
            {"results": [{"id": "block-1", "type": "paragraph"}]},
        ]
    )
    skill = NotionSkill(adapter)
    await skill.execute(
        call(
            "integration.notion",
            {"operation": "search", "query": "planning", "object_type": "page"},
        ),
        asyncio.Event(),
    )
    await skill.execute(
        call("integration.notion", {"operation": "get_page", "resource_id": "page-1"}),
        asyncio.Event(),
    )
    await skill.execute(
        call("integration.notion", {"operation": "list_children", "resource_id": "page-1"}),
        asyncio.Event(),
    )

    assert adapter.requests[0]["method"] == "POST"
    assert adapter.requests[0]["body"]["filter"] == {"property": "object", "value": "page"}
    assert adapter.requests[0]["safe_to_retry"] is True
    assert adapter.requests[1]["path"] == "/pages/page-1"
    assert adapter.requests[2]["path"] == "/blocks/page-1/children"


@pytest.mark.asyncio
async def test_github_reads_use_search_envelopes_and_fixed_repository_paths():
    adapter = RecordingAdapter(
        [
            {"items": [{"id": 1, "full_name": "openai/assistant", "description": "Private"}], "total_count": 30},
            {"full_name": "openai/assistant", "description": "Private"},
            {"items": [{"number": 7, "title": "Bug", "html_url": "https://github.com/openai/assistant/issues/7"}], "total_count": 1},
        ]
    )
    skill = GitHubSkill(adapter)
    first = await skill.execute(
        call("integration.github", {"operation": "search_repositories", "query": "assistant"}),
        asyncio.Event(),
    )
    await skill.execute(
        call(
            "integration.github",
            {"operation": "get_repository", "owner": "openai", "repo": "assistant"},
        ),
        asyncio.Event(),
    )
    await skill.execute(
        call(
            "integration.github",
            {"operation": "list_issues", "owner": "openai", "repo": "assistant"},
        ),
        asyncio.Event(),
    )

    assert adapter.requests[0]["path"] == "/search/repositories"
    assert first.data["next_page"] == 2
    assert adapter.requests[1]["path"] == "/repos/openai/assistant"
    assert adapter.requests[2]["query"]["q"] == "repo:openai/assistant is:issue"


@pytest.mark.asyncio
async def test_api_token_connections_probe_and_keep_provider_secrets_separate():
    def opener(request, timeout):
        if request.full_url.endswith("/auth.test"):
            return Response({"ok": True, "team": "Example Workspace"})
        if request.full_url.endswith("/users/me"):
            return Response({"object": "user", "name": "Notion Bot", "id": "bot-1"})
        if request.full_url.endswith("/user"):
            return Response({"login": "octocat"})
        raise AssertionError(request.full_url)

    slack_vault = MemoryVault()
    notion_vault = MemoryVault()
    github_vault = MemoryVault()
    slack = SlackConnectionService(slack_vault, opener=opener)
    notion = NotionConnectionService(notion_vault, opener=opener)
    github = GitHubConnectionService(github_vault, opener=opener)

    assert (await slack.connect("slack-token")).account == "Example Workspace"
    assert (await notion.connect("notion-token")).account == "Notion Bot"
    assert (await github.connect("github-token")).account == "octocat"
    assert slack_vault.get("slack_api_token") == "slack-token"
    assert notion_vault.get("notion_api_token") == "notion-token"
    assert github_vault.get("github_api_token") == "github-token"
    assert "notion_api_token" not in slack_vault.values
    assert "github_api_token" not in notion_vault.values

    await slack.disconnect()
    assert slack_vault.get("slack_api_token") is None


@pytest.mark.asyncio
async def test_readonly_platform_extensions_are_visible_installable_and_restartable(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        offline_lock=False,
        embedding_provider="lexical",
        llm_provider="none",
    )
    container = build_container(settings)
    extension_ids = {
        "integration.slack": "slack_api_token",
        "integration.notion": "notion_api_token",
        "integration.github": "github_api_token",
    }
    for extension_id in extension_ids:
        assert any(item.extension_id == extension_id for item in container.extension_hub.catalog.extensions)
        await container.extension_hub.install(extension_id)
    connection = container.extension_hub.connection_service("integration.slack")
    for secret_name in extension_ids.values():
        connection.vault.set(secret_name, f"{secret_name}-value")

    restarted = build_container(settings)
    for extension_id in extension_ids:
        assert restarted.registry.is_enabled(extension_id) is True
