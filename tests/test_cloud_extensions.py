from __future__ import annotations

import asyncio
import io
import json

import pytest

from assistant.bootstrap import _managed_extension_registry, build_container
from assistant.config import Settings
from assistant.contracts import PermissionContext, RiskLevel, SkillCall
from assistant.integrations.anthropic import (
    ANTHROPIC_API_KEY_SECRET,
    AnthropicAdapter,
    AnthropicConnectionService,
)
from assistant.integrations.openai import (
    OPENAI_API_KEY_SECRET,
    OpenAIAdapter,
    OpenAIConnectionService,
)
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.anthropic import AnthropicInput, AnthropicSkill
from assistant.skills.openai import OpenAIInput, OpenAISkill


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


def call(skill_id: str, arguments: dict) -> SkillCall:
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


def test_cloud_routes_are_provider_extensions_and_report_setup_state():
    router = DeterministicRouter()
    chatgpt = router.route("ask ChatGPT explain asyncio").intent
    assert chatgpt.name == "integration.openai"
    assert chatgpt.arguments == {"question": "explain asyncio", "max_tokens": 1_500}

    codex = router.route("ask Codex review this function").intent
    assert codex.name == "integration.openai"
    assert codex.arguments == {
        "provider": "codex",
        "question": "review this function",
        "max_tokens": 1_500,
    }

    claude = router.route("ask Claude for a short summary").intent
    assert claude.name == "integration.anthropic"
    assert claude.arguments == {"question": "a short summary", "max_tokens": 1_500}

    unavailable = DeterministicRouter(skill_status=lambda _skill_id: "unavailable")
    guidance = unavailable.route("ask OpenAI explain this").intent
    assert guidance.name == "conversation.answer"
    assert "Extension Hub" in guidance.arguments["text"]

    disabled = DeterministicRouter(skill_status=lambda _skill_id: "disabled")
    guidance = disabled.route("ask Claude explain this").intent
    assert "Skills" in guidance.arguments["text"]

    def provider_status(_skill_id, *, connection_required=True):
        return "disconnected" if connection_required else "enabled"

    local_mode = DeterministicRouter(skill_status=provider_status)
    assert local_mode.route("ask Codex explain this").intent.name == "integration.openai"
    guidance = local_mode.route("ask OpenAI explain this").intent
    assert "API key" in guidance.arguments["text"]


def test_cloud_inputs_are_strict_and_manifests_are_foreground_only():
    with pytest.raises(ValueError):
        OpenAIInput(question="hello", unexpected=True)
    with pytest.raises(ValueError):
        AnthropicInput(question="hello", model="claude\nmodel")

    openai = OpenAISkill(SimpleAdapter("OpenAI answer"))
    anthropic = AnthropicSkill(SimpleAdapter("Claude answer"))
    for skill, skill_id, origin in (
        (openai, "integration.openai", "https://api.openai.com"),
        (anthropic, "integration.anthropic", "https://api.anthropic.com"),
    ):
        assert skill.manifest.skill_id == skill_id
        assert skill.manifest.risk == RiskLevel.LOW
        assert skill.manifest.safe_to_retry is False
        assert skill.manifest.background_mode.value == "foreground_only"
        assert skill.manifest.permissions[0].targets == (origin,)
        assert skill.required_permissions(
            call(skill_id, {"question": "hello"})
        )[0].targets == (origin,)


@pytest.mark.asyncio
async def test_provider_adapters_use_fixed_paths_and_provider_auth_headers():
    openai_requests = []

    def openai_opener(request, timeout):
        openai_requests.append(request)
        return Response(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OpenAI answer"}],
                    }
                ]
            }
        )

    openai_tokens = SimpleTokens("openai-key")
    answer = await OpenAIAdapter(openai_tokens, opener=openai_opener).ask(
        "gpt-test", "Hello", 42
    )
    assert answer == "OpenAI answer"
    request = openai_requests[0]
    assert request.full_url == "https://api.openai.com/v1/responses"
    assert request.headers["Authorization"] == "Bearer openai-key"
    assert json.loads(request.data) == {
        "model": "gpt-test",
        "input": "Hello",
        "max_output_tokens": 42,
        "store": False,
    }

    anthropic_requests = []

    def anthropic_opener(request, timeout):
        anthropic_requests.append(request)
        return Response({"content": [{"type": "text", "text": "Claude answer"}]})

    answer = await AnthropicAdapter(SimpleTokens("anthropic-key"), opener=anthropic_opener).ask(
        "claude-test", "Hello", 42
    )
    assert answer == "Claude answer"
    request = anthropic_requests[0]
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.headers["X-api-key"] == "anthropic-key"
    assert "Authorization" not in request.headers
    assert request.headers["Anthropic-version"] == "2023-06-01"
    assert json.loads(request.data)["messages"] == [{"role": "user", "content": "Hello"}]


@pytest.mark.asyncio
async def test_cloud_skills_return_sensitive_typed_answers_and_honor_cancellation():
    adapter = SimpleAdapter("private answer")
    skill = OpenAISkill(adapter, default_model="gpt-default")
    result = await skill.execute(
        call("integration.openai", {"question": "Hello", "max_tokens": 9}),
        asyncio.Event(),
    )
    assert result.data == {
        "provider": "openai",
        "model": "gpt-default",
        "answer": "private answer",
    }
    assert result.display_text == "private answer"
    assert result.sensitive is True
    assert adapter.ask_calls == [("gpt-default", "Hello", 9)]

    codex = SimpleCodexClient("local answer")
    codex_skill = OpenAISkill(
        adapter,
        codex_client=codex,
        default_codex_model="gpt-codex",
    )
    result = await codex_skill.execute(
        call(
            "integration.openai",
            {"provider": "codex", "question": "Review this", "max_tokens": 11},
        ),
        asyncio.Event(),
    )
    assert result.data == {
        "provider": "codex",
        "model": "gpt-codex",
        "answer": "local answer",
    }
    assert codex.ask_calls == [("Review this", "gpt-codex")]
    assert {permission.capability for permission in codex_skill.required_permissions(
        call("integration.openai", {"provider": "codex", "question": "Review this"})
    )} == {"network", "process.launch"}

    cancelled = asyncio.Event()
    cancelled.set()
    result = await AnthropicSkill(adapter).execute(
        call("integration.anthropic", {"question": "Hello"}),
        cancelled,
    )
    assert result.status.value == "cancelled"
    assert len(adapter.ask_calls) == 1


@pytest.mark.asyncio
async def test_cloud_connections_probe_and_managed_lifecycle(tmp_path):
    def opener(request, timeout):
        assert request.full_url in {
            "https://api.openai.com/v1/models",
            "https://api.anthropic.com/v1/models",
        }
        return Response({"data": []})

    openai_vault = MemoryVault()
    anthropic_vault = MemoryVault()
    openai = OpenAIConnectionService(openai_vault, opener=opener)
    anthropic = AnthropicConnectionService(anthropic_vault, opener=opener)
    assert (await openai.connect("openai-key")).usable is True
    assert (await anthropic.connect("anthropic-key")).usable is True
    assert openai_vault.get(OPENAI_API_KEY_SECRET) == "openai-key"
    assert anthropic_vault.get(ANTHROPIC_API_KEY_SECRET) == "anthropic-key"
    await openai.disconnect()
    assert openai_vault.get(OPENAI_API_KEY_SECRET) is None

    legacy = MemoryVault({"openai": "legacy-key", "anthropic": "legacy-key-2"})
    managed = _managed_extension_registry(legacy)
    assert legacy.get(OPENAI_API_KEY_SECRET) == "legacy-key"
    assert legacy.get(ANTHROPIC_API_KEY_SECRET) == "legacy-key-2"
    assert legacy.get("openai") is None
    assert legacy.get("anthropic") is None
    assert {item.extension_id for item in managed.catalog_extensions} >= {
        "integration.openai",
        "integration.anthropic",
    }

    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        offline_lock=False,
        embedding_provider="lexical",
        llm_provider="none",
    )
    container = build_container(settings)
    assert container.registry.inspect("cloud.ask").manifest.skill_id == "cloud.ask"
    assert container.registry.inspect("cloud.ask").input_schema()["properties"]["provider"][
        "default"
    ] == "codex"
    await container.extension_hub.install("integration.openai")
    assert container.extension_hub.connection_binding(
        "integration.openai"
    ).connection_required is False
    assert container.extension_hub.sync_connection_state("integration.openai") is False
    assert container.registry.is_enabled("integration.openai")
    await container.extension_hub.install("integration.anthropic")
    container.extension_hub.connection_service("integration.openai").vault.set(
        OPENAI_API_KEY_SECRET, "openai-key"
    )
    container.extension_hub.connection_service("integration.anthropic").vault.set(
        ANTHROPIC_API_KEY_SECRET, "anthropic-key"
    )
    assert set(container.extension_hub.sync_connected_extensions()) >= {
        "integration.openai",
        "integration.anthropic",
    }
    assert container.registry.is_enabled("integration.openai")
    assert container.registry.is_enabled("integration.anthropic")


class SimpleTokens:
    def __init__(self, token: str):
        self.token = token

    async def access_token(self, *, force_refresh=False):
        return self.token


class SimpleAdapter:
    def __init__(self, answer: str):
        self.answer = answer
        self.ask_calls = []

    async def ask(self, model: str, question: str, max_tokens: int) -> str:
        self.ask_calls.append((model, question, max_tokens))
        return self.answer

    async def health(self):
        return {"healthy": True}


class SimpleCodexClient:
    def __init__(self, answer: str):
        self.answer = answer
        self.ask_calls = []

    async def ask(self, question: str, *, model=None, cancel_event=None) -> str:
        self.ask_calls.append((question, model))
        return self.answer
