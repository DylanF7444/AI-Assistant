import json

import pytest

from assistant.language.ollama import OllamaProvider


class FakeHttpResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_completion_bounds_prompt_without_discarding_rules_or_request(monkeypatch, capsys):
    raw_response = '{"answer":"exact model response"}'
    submitted_prompt = ""

    def fake_urlopen(request, timeout):
        nonlocal submitted_prompt
        payload = json.loads(request.data)
        submitted_prompt = payload["prompt"]
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
