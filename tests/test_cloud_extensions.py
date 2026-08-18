from __future__ import annotations

import asyncio
import io
import json

import pytest

from assistant.bootstrap import _managed_extension_registry, build_container
from assistant.config import Settings
from assistant.contracts import (
    PermissionContext,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillError,
    SkillResult,
)
from assistant.execution.engine import ExecutionEngine
from assistant.integrations.anthropic import (
    ANTHROPIC_API_KEY_SECRET,
    AnthropicAdapter,
    AnthropicConnectionService,
)
from assistant.integrations.cloud import CloudPricing, TokenPricing
from assistant.integrations.openai import (
    OPENAI_API_KEY_SECRET,
    OpenAIAdapter,
    OpenAIConnectionService,
    OpenAIError,
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

    explicit = router.route("ask OpenAI or Claude explain cancellation").intent
    assert explicit.name == "integration.openai"
    assert explicit.arguments == {
        "provider": "openai",
        "question": "explain cancellation",
        "max_tokens": 1_500,
        "fallback_provider": "anthropic",
    }
    ordinary = router.route("ask OpenAI explain cancellation").intent
    assert "fallback_provider" not in ordinary.arguments


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
        assert skill.required_permissions(call(skill_id, {"question": "hello"}))[0].targets == (
            origin,
        )


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
    answer = await OpenAIAdapter(openai_tokens, opener=openai_opener).ask("gpt-test", "Hello", 42)
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

    def forbidden_opener(request, timeout):
        raise AssertionError("invalid direct adapter input reached the network")

    with pytest.raises(OpenAIError) as error:
        await OpenAIAdapter(SimpleTokens("openai-key"), opener=forbidden_opener).ask(
            "gpt-test", "bad\x00prompt", 42
        )
    assert error.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_provider_usage_is_typed_and_cost_estimates_are_explicitly_configured():
    def opener(request, timeout):
        return Response(
            {
                "output_text": "Measured answer",
                "usage": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
            }
        )

    answer = await OpenAIAdapter(
        SimpleTokens("openai-key"),
        opener=opener,
        pricing=CloudPricing({("openai", "gpt-test"): TokenPricing(1.0, 2.0)}),
    ).ask_with_usage("gpt-test", "Hello", 42)
    assert answer.text == "Measured answer"
    assert answer.usage is not None
    assert answer.usage.as_dict() == {
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
        "estimated_cost_usd": 0.00015,
        "cost_basis": "configured",
    }


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
    assert {
        permission.capability
        for permission in codex_skill.required_permissions(
            call("integration.openai", {"provider": "codex", "question": "Review this"})
        )
    } == {"network", "process.launch"}

    cancelled = asyncio.Event()
    cancelled.set()
    result = await AnthropicSkill(adapter).execute(
        call("integration.anthropic", {"question": "Hello"}),
        cancelled,
    )
    assert result.status.value == "cancelled"
    assert len(adapter.ask_calls) == 1


@pytest.mark.asyncio
async def test_cloud_fallback_is_explicit_and_only_transient_failures_retry():
    class UnavailableAdapter(SimpleAdapter):
        async def ask(self, model: str, question: str, max_tokens: int) -> str:
            self.ask_calls.append((model, question, max_tokens))
            raise OpenAIError(503, "temporarily unavailable", recoverable=True)

    primary = UnavailableAdapter("unused")
    fallback = SimpleAdapter("fallback answer")
    skill = OpenAISkill(
        primary,
        anthropic_adapter=fallback,
        default_anthropic_model="claude-test",
    )
    result = await skill.execute(
        call(
            "integration.openai",
            {"question": "Keep this private", "fallback_provider": "anthropic"},
        ),
        asyncio.Event(),
    )
    assert result.status.value == "completed"
    assert result.data["provider"] == "anthropic"
    assert result.data["fallback_used"] is True
    assert result.data["attempts"] == [
        {
            "provider": "openai",
            "model": "gpt-5.5",
            "outcome": "failed",
            "error_code": "provider_unavailable",
        },
        {"provider": "anthropic", "model": "claude-test", "outcome": "completed"},
    ]

    class AuthFailure(SimpleAdapter):
        async def ask(self, model: str, question: str, max_tokens: int) -> str:
            raise OpenAIError(401, "invalid key")

    auth_fallback = SimpleAdapter("must not run")
    result = await OpenAISkill(
        AuthFailure("unused"),
        anthropic_adapter=auth_fallback,
    ).execute(
        call(
            "integration.openai",
            {
                "question": "No implicit credential bypass",
                "fallback_provider": "anthropic",
            },
        ),
        asyncio.Event(),
    )
    assert result.status.value == "failed"
    assert result.error and result.error.code == "authentication_failed"
    assert auth_fallback.ask_calls == []


@pytest.mark.asyncio
async def test_cloud_fallback_requires_an_enabled_provider_and_rejects_oversized_answers():
    class UnavailableAdapter(SimpleAdapter):
        async def ask(self, model: str, question: str, max_tokens: int) -> str:
            self.ask_calls.append((model, question, max_tokens))
            raise OpenAIError(503, "temporarily unavailable", recoverable=True)

    fallback = SimpleAdapter("must not run")
    result = await OpenAISkill(
        UnavailableAdapter("unused"),
        anthropic_adapter=fallback,
        provider_availability={"openai": True, "anthropic": False, "codex": True},
    ).execute(
        call(
            "integration.openai",
            {"question": "Keep this private", "fallback_provider": "anthropic"},
        ),
        asyncio.Event(),
    )
    assert result.status.value == "failed"
    assert result.error and result.error.code == "not_available"
    assert result.data["attempts"][-1]["error_code"] == "not_available"
    assert fallback.ask_calls == []

    oversized = await OpenAISkill(SimpleAdapter("x" * 100_001)).execute(
        call("integration.openai", {"question": "Return a bounded answer"}),
        asyncio.Event(),
    )
    assert oversized.status.value == "failed"
    assert oversized.error and oversized.error.code == "invalid_response"


@pytest.mark.asyncio
async def test_cloud_skill_blocks_offline_requests_before_client_invocation():
    adapter = SimpleAdapter("must not run")
    result = await OpenAISkill(adapter, offline_lock=True).execute(
        call("integration.openai", {"question": "Do not send this"}),
        asyncio.Event(),
    )
    assert result.status.value == "failed"
    assert result.error and result.error.code == "offline_locked"
    assert adapter.ask_calls == []


@pytest.mark.asyncio
async def test_codex_login_failure_points_to_chatgpt_authentication():
    class NotLoggedIn:
        async def ask(self, question: str, *, model=None, cancel_event=None) -> str:
            raise RuntimeError("Codex is not logged in; run codex login")

    result = await OpenAISkill(
        SimpleAdapter("unused"),
        codex_client=NotLoggedIn(),
    ).execute(
        call("integration.openai", {"provider": "codex", "question": "Review this"}),
        asyncio.Event(),
    )

    assert result.status.value == "failed"
    assert result.error and result.error.code == "not_connected"
    assert "codex login" in result.summary


@pytest.mark.asyncio
async def test_codex_unknown_model_points_to_model_setting():
    class UnknownModel:
        async def ask(self, question: str, *, model=None, cancel_event=None) -> str:
            raise RuntimeError(f"Unknown model {model} is used")

    result = await OpenAISkill(
        SimpleAdapter("unused"),
        codex_client=UnknownModel(),
    ).execute(
        call(
            "integration.openai",
            {
                "provider": "codex",
                "question": "Review this",
                "model": "retired-codex-model",
            },
        ),
        asyncio.Event(),
    )

    assert result.status.value == "failed"
    assert result.error and result.error.code == "invalid_request"
    assert "model" in result.summary.casefold()
    assert "temporarily unavailable" not in result.summary.casefold()


@pytest.mark.asyncio
async def test_codex_non_repository_failure_is_not_reported_as_provider_outage():
    class NonRepository:
        async def ask(self, question: str, *, model=None, cancel_event=None) -> str:
            raise RuntimeError(
                "Not inside a trusted directory and --skip-git-repo-check was not specified."
            )

    result = await OpenAISkill(
        SimpleAdapter("unused"),
        codex_client=NonRepository(),
    ).execute(
        call("integration.openai", {"provider": "codex", "question": "Explain this"}),
        asyncio.Event(),
    )

    assert result.status.value == "failed"
    assert result.error and result.error.code == "invalid_request"
    assert "working directory" in result.summary.casefold()
    assert "temporarily unavailable" not in result.summary.casefold()


def test_cloud_audit_payload_omits_prompt_and_answer_but_keeps_safe_usage_metadata():
    request = call(
        "integration.openai",
        {
            "question": "private prompt that must not persist",
            "model": "gpt-test",
            "fallback_provider": "anthropic",
        },
    )
    result = SkillResult(
        status=ResultStatus.COMPLETED,
        summary="Answered.",
        display_text="private answer that must not persist",
        speech_text="private answer that must not persist",
        data={
            "provider": "anthropic",
            "model": "claude-test",
            "answer": "private answer that must not persist",
            "usage": {"total_tokens": 10, "cost_basis": "unavailable"},
        },
        sensitive=True,
    )
    arguments = ExecutionEngine._audit_arguments(request)
    audited = ExecutionEngine._audit_result(result, skill_id=request.skill_id)
    assert arguments == {
        "model": "gpt-test",
        "fallback_provider": "anthropic",
        "prompt_omitted": True,
    }
    assert "private prompt" not in str(arguments)
    assert "private answer" not in str(audited)
    assert audited["data"] == {
        "provider": "anthropic",
        "model": "claude-test",
        "usage": {"total_tokens": 10, "cost_basis": "unavailable"},
    }

    failed = SkillResult(
        status=ResultStatus.FAILED,
        summary="private provider response",
        error=SkillError(
            code="skill_failed",
            message="private provider traceback and prompt",
            recoverable=False,
        ),
        rollback_token="private rollback token",
        sensitive=True,
    )
    failed_audit = ExecutionEngine._audit_result(failed, skill_id=request.skill_id)
    assert "private" not in str(failed_audit)
    assert failed_audit["summary"] == "[CLOUD_RESULT_OMITTED]"
    assert failed_audit["error"] == {
        "code": "skill_failed",
        "message": "[CLOUD_ERROR_OMITTED]",
        "recoverable": False,
    }
    assert failed_audit["rollback_token"] is None


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
    assert (
        container.registry.inspect("cloud.ask").input_schema()["properties"]["provider"]["default"]
        == "codex"
    )
    await container.extension_hub.install("integration.openai")
    assert (
        container.extension_hub.connection_binding("integration.openai").connection_required
        is False
    )
    assert container.extension_hub.sync_connection_state("integration.openai") is False
    assert container.registry.is_enabled("integration.openai")
    await container.extension_hub.install("integration.anthropic")
    assert container.extension_hub.sync_connection_state("integration.anthropic") is False
    assert container.registry.is_enabled("integration.anthropic")
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

    container.extension_hub.set_offline_lock(True)
    assert container.registry.inspect("cloud.ask").offline_lock is True
    assert container.registry.inspect("integration.openai").offline_lock is True
    assert container.registry.inspect("integration.anthropic").offline_lock is True
    assert container.extension_hub.connection_status("integration.openai").healthy is False


def test_cloud_pricing_settings_are_validated_and_loaded(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        cloud_pricing={
            "openai:gpt-test": {
                "input_usd_per_million": 1.0,
                "output_usd_per_million": 2.0,
            }
        },
    )
    assert settings.cloud_pricing["openai:gpt-test"]["output_usd_per_million"] == 2.0
    with pytest.raises(ValueError, match="provider:model"):
        Settings(profile="testing", data_dir=tmp_path, cloud_pricing={"gpt-test": {}})
    with pytest.raises(ValueError, match="Unsupported cloud pricing provider"):
        Settings(
            profile="testing",
            data_dir=tmp_path,
            cloud_pricing={
                "google:gemini": {
                    "input_usd_per_million": 1.0,
                    "output_usd_per_million": 2.0,
                }
            },
        )
    with pytest.raises(ValueError, match="unsupported fields"):
        Settings(
            profile="testing",
            data_dir=tmp_path,
            cloud_pricing={
                "openai:gpt-test": {
                    "input_usd_per_million": 1.0,
                    "output_usd_per_million": 2.0,
                    "currency": "USD",
                }
            },
        )
    with pytest.raises(ValueError, match="finite and non-negative"):
        TokenPricing(True, 1.0)
    with pytest.raises(ValueError, match="needs input_usd_per_million"):
        CloudPricing.from_mapping(
            {
                "openai:gpt-test": {
                    "input_usd_per_million": True,
                    "output_usd_per_million": 2.0,
                }
            }
        )


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
