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
async def test_completion_debug_prints_exact_submitted_prompt_and_raw_response(
    monkeypatch, capsys
):
    raw_response = '{"answer":"exact model response"}'

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data)
        assert payload["prompt"] == "p" * 8000
        assert timeout == 3
        return FakeHttpResponse({"response": raw_response})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OllamaProvider("test-model", terminal_debug=True)

    result = await provider.structured_completion(
        "omitted-prefix" + "p" * 8000,
        {"type": "object"},
        timeout_seconds=3,
    )

    assert result == {"answer": "exact model response"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[assistant ai debug]" in captured.err
    assert "model=test-model" in captured.err
    assert f"prompt:\n{'p' * 8000}\nresponse:\n{raw_response}" in captured.err
    assert "omitted-prefix" not in captured.err


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
