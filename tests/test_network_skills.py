from __future__ import annotations

import asyncio
import io
import json

import pytest

from assistant.contracts import PermissionContext, SkillCall
from assistant.integrations.web import CloudModelClient, WebClient
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.network import CloudAskSkill, WebSearchSkill
from assistant.storage.secrets import SecretVault


class FakeHeaders:
    def __init__(self, content_type: str = "application/json") -> None:
        self.content_type = content_type

    def get_content_type(self):
        return self.content_type

    def get_content_charset(self):
        return "utf-8"


class FakeResponse(io.BytesIO):
    def __init__(self, payload: dict, content_type: str = "application/json"):
        super().__init__(json.dumps(payload).encode())
        self.headers = FakeHeaders(content_type)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class FakeOpener:
    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return FakeResponse(self.responses.pop(0))


def call(skill_id: str, arguments: dict) -> SkillCall:
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


def test_network_routes_are_explicit():
    router = DeterministicRouter()
    assert router.route("search the web for current Python news").intent.name == "web.search"
    claude = router.route("ask Claude explain asyncio").intent
    assert claude.name == "cloud.ask"
    assert claude.arguments["provider"] == "anthropic"
    codex = router.route("ask Codex review this function").intent
    assert codex.arguments["provider"] == "codex"


@pytest.mark.asyncio
async def test_duckduckgo_search_is_default_and_parses_nested_topics(monkeypatch):
    monkeypatch.setattr(
        "assistant.integrations.web._validate_public_https", lambda _url: None
    )
    opener = FakeOpener(
        [
            {
                "Heading": "Python",
                "AbstractText": "A programming language.",
                "AbstractURL": "https://example.com/python",
                "Results": [],
                "RelatedTopics": [
                    {
                        "Name": "Topics",
                        "Topics": [
                            {
                                "FirstURL": "https://example.com/asyncio",
                                "Text": "asyncio - Asynchronous I/O",
                            }
                        ],
                    }
                ],
            }
        ]
    )
    skill = WebSearchSkill(WebClient(opener), lambda _name: None)
    result = await skill.execute(
        call("web.search", {"query": "python", "count": 2}), asyncio.Event()
    )
    assert [item["title"] for item in result.data["results"]] == [
        "Python",
        "asyncio",
    ]
    request = opener.requests[0]
    assert request.full_url.startswith("https://api.duckduckgo.com/?")
    assert "format=json" in request.full_url
    assert "X-subscription-token" not in request.headers


@pytest.mark.asyncio
async def test_brave_search_remains_available(monkeypatch):
    monkeypatch.setattr(
        "assistant.integrations.web._validate_public_https", lambda _url: None
    )
    opener = FakeOpener(
        [{"web": {"results": [{"title": "Result", "url": "https://example.com", "description": "Text"}]}}]
    )
    skill = WebSearchSkill(
        WebClient(opener), lambda _name: "brave-key", provider="brave"
    )
    result = await skill.execute(
        call("web.search", {"query": "test", "count": 1}), asyncio.Event()
    )
    assert result.data["results"][0]["title"] == "Result"
    assert opener.requests[0].headers["X-subscription-token"] == "brave-key"
    assert skill.manifest.permissions[0].capability == "network"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "response", "expected"),
    [
        (
            "openai",
            {"output": [{"type": "message", "content": [{"type": "output_text", "text": "OpenAI answer"}]}]},
            "OpenAI answer",
        ),
        (
            "anthropic",
            {"content": [{"type": "text", "text": "Claude answer"}]},
            "Claude answer",
        ),
    ],
)
async def test_cloud_ask_parses_provider_responses(
    monkeypatch, provider, response, expected
):
    monkeypatch.setattr(
        "assistant.integrations.web._validate_public_https", lambda _url: None
    )
    opener = FakeOpener([response])
    skill = CloudAskSkill(
        CloudModelClient(opener),
        lambda _name: "api-key",
        {"openai": "openai-model", "anthropic": "claude-model"},
    )
    result = await skill.execute(
        call("cloud.ask", {"provider": provider, "question": "Hello"}),
        asyncio.Event(),
    )
    assert result.display_text == expected
    assert result.sensitive


@pytest.mark.asyncio
async def test_codex_uses_openai_key_and_configured_codex_model(monkeypatch):
    monkeypatch.setattr(
        "assistant.integrations.web._validate_public_https", lambda _url: None
    )
    opener = FakeOpener(
        [{"output": [{"type": "message", "content": [{"type": "output_text", "text": "Review"}]}]}]
    )
    requested_keys = []
    skill = CloudAskSkill(
        CloudModelClient(opener),
        lambda name: requested_keys.append(name) or "api-key",
        {
            "openai": "openai-model",
            "codex": "gpt-5.3-codex",
            "anthropic": "claude-model",
        },
    )
    result = await skill.execute(
        call("cloud.ask", {"provider": "codex", "question": "Review this"}),
        asyncio.Event(),
    )
    assert requested_keys == ["openai"]
    assert result.data["model"] == "gpt-5.3-codex"


class ReversibleCipher:
    def encrypt(self, plaintext: str) -> bytes:
        return plaintext[::-1].encode()

    def decrypt(self, ciphertext: bytes) -> str:
        return ciphertext.decode()[::-1]


def test_secret_vault_persists_encrypted_values(tmp_path):
    vault = SecretVault(tmp_path, ReversibleCipher())
    vault.set("openai", "secret-key")
    assert vault.has("openai")
    assert vault.get("openai") == "secret-key"
    assert b"secret-key" not in (tmp_path / "openai.bin").read_bytes()
    vault.delete("openai")
    assert vault.get("openai") is None
