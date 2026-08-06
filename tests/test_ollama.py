import json

import pytest

from assistant.bootstrap import _record_ollama_usage, build_container
from assistant.language.ollama import OllamaProvider, OllamaUsage


class FakeHttpResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_default_prompt_budget_can_hold_grounded_multi_skill_context():
    provider = OllamaProvider("test-model")

    assert provider.max_prompt_chars == 12000


@pytest.mark.asyncio
async def test_completion_bounds_prompt_without_discarding_rules_or_request(monkeypatch, capsys):
    raw_response = '{"answer":"exact model response"}'
    submitted_prompt = ""

    def fake_urlopen(request, timeout):
        nonlocal submitted_prompt
        payload = json.loads(request.data)
        submitted_prompt = payload["prompt"]
        assert payload["keep_alive"] == "30m"
        assert payload["think"] is False
        assert len(submitted_prompt) == 120
        assert submitted_prompt.startswith("BEGIN-RULES")
        assert submitted_prompt.endswith("END-REQUEST")
        assert "[Earlier planner context omitted for local inference.]" in submitted_prompt
        assert timeout == 3
        return FakeHttpResponse({"response": raw_response})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OllamaProvider("test-model", max_prompt_chars=120, terminal_debug=True)

    result = await provider.structured_completion(
        "BEGIN-RULES" + "p" * 300 + "END-REQUEST",
        {"type": "object"},
        timeout_seconds=3,
    )

    assert result == {"answer": "exact model response"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[assistant ai debug]" in captured.err
    assert "model=test-model" in captured.err
    assert f"prompt:\n{submitted_prompt}\nresponse:\n{raw_response}" in captured.err


@pytest.mark.asyncio
async def test_completion_debug_is_silent_when_disabled(monkeypatch, capsys):
    def fake_urlopen(_request, timeout):
        assert timeout == 3
        return FakeHttpResponse({"response": '{"answer":"quiet"}'})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OllamaProvider("test-model", terminal_debug=False)

    await provider.structured_completion(
        "actual prompt",
        {"type": "object"},
        timeout_seconds=3,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.asyncio
async def test_completion_retries_truncated_json_with_concise_output_instructions(monkeypatch):
    payloads = []
    responses = iter(
        (
            {
                "response": (
                    '{"disposition":"answer","skill_id":"web.headless_read",'
                    '"arguments":{"url":"https://slickdeals.net"},'
                    '"answer":"I will fetch it shortly. #Deals'
                ),
                "done": True,
                "done_reason": "length",
            },
            {
                "response": (
                    '{"disposition":"skill","skill_id":"web.headless_read",'
                    '"arguments":{"url":"https://slickdeals.net"},'
                    '"answer":null,"confidence":0.9,"clarification":null}'
                ),
                "done": True,
                "done_reason": "stop",
            },
        )
    )

    def fake_urlopen(request, timeout):
        assert timeout == 3
        payloads.append(json.loads(request.data))
        return FakeHttpResponse(next(responses))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OllamaProvider("test-model", max_tokens=256, max_prompt_chars=1000)

    result = await provider.structured_completion(
        "Choose a skill for the request.",
        {"type": "object"},
        timeout_seconds=3,
    )

    assert result["disposition"] == "skill"
    assert result["skill_id"] == "web.headless_read"
    assert len(payloads) == 2
    assert payloads[0]["options"]["num_predict"] == 256
    assert payloads[1]["options"]["num_predict"] == 512
    assert "previous response was truncated or malformed" in payloads[1]["prompt"]
    assert "Do not include commentary, emoji, or hashtags" in payloads[1]["prompt"]


@pytest.mark.asyncio
async def test_completion_reports_native_usage_with_metric_context(monkeypatch):
    submitted = {}
    usages = []

    def fake_urlopen(request, timeout):
        submitted.update(json.loads(request.data))
        assert timeout == 4
        return FakeHttpResponse(
            {
                "response": '{"answer":"Paris"}',
                "total_duration": 2_000_000,
                "load_duration": 300_000,
                "prompt_eval_count": 40,
                "prompt_eval_duration": 500_000,
                "eval_count": 8,
                "eval_duration": 900_000,
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OllamaProvider(
        "test-model",
        keep_alive="45m",
        usage_callback=usages.append,
    )

    with (
        provider.metric_scope("request-1"),
        provider.operation_scope("grounded_decision"),
        provider.token_budget_scope(192),
    ):
        result = await provider.structured_completion(
            "Answer.",
            {"type": "object"},
            timeout_seconds=4,
        )

    assert result == {"answer": "Paris"}
    assert submitted["keep_alive"] == "45m"
    assert submitted["options"]["num_predict"] == 192
    assert len(usages) == 1
    usage = usages[0]
    assert usage.correlation_id == "request-1"
    assert usage.operation == "grounded_decision"
    assert usage.total_duration_ms == 2
    assert usage.load_duration_ms == 0.3
    assert usage.prompt_eval_duration_ms == 0.5
    assert usage.generation_duration_ms == 0.9
    assert usage.prompt_tokens == 40
    assert usage.output_tokens == 8


@pytest.mark.asyncio
async def test_preload_keeps_model_resident_without_generating(monkeypatch):
    submitted = {}

    def fake_urlopen(request, timeout):
        submitted.update(json.loads(request.data))
        assert timeout == 5
        return FakeHttpResponse({"response": "", "total_duration": 1_000_000})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OllamaProvider("test-model", keep_alive="2h")

    await provider.preload(timeout_seconds=5)

    assert submitted == {
        "model": "test-model",
        "stream": False,
        "keep_alive": "2h",
    }


def test_preload_metrics_do_not_pollute_completion_latency(settings):
    container = build_container(settings)
    _record_ollama_usage(
        container.metrics,
        OllamaUsage(
            correlation_id=None,
            operation="preload",
            wall_duration_ms=12,
            total_duration_ms=10,
            load_duration_ms=8,
            prompt_eval_duration_ms=0,
            generation_duration_ms=0,
            prompt_tokens=None,
            output_tokens=None,
        ),
    )

    stages = [row["stage"] for row in container.metrics.query(limit=10)]
    assert "model.preload" in stages
    assert "model.total" not in stages
