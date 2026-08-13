from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant.integrations.api_token import ApiTokenConnectionService, ApiTokenProvider
from assistant.integrations.http import ExternalJsonClient
from assistant.storage.secrets import SecretVault

API_ROOT = "https://api.openai.com/v1"
OPENAI_API_KEY_SECRET = "openai_api_key"
OPENAI_LEGACY_SECRET_NAMES = ("openai", "openai_api_token")
MAX_ANSWER_CHARACTERS = 100_000


class OpenAIError(RuntimeError):
    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"OpenAI request failed ({status}): {self.reason}")


def _openai_error(status: int, reason: str, recoverable: bool) -> OpenAIError:
    return OpenAIError(status, reason, recoverable=recoverable)


class OpenAIAdapter:
    """Bounded OpenAI Responses API adapter for the reviewed extension."""

    def __init__(
        self,
        tokens: ApiTokenProvider,
        *,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        kwargs: dict[str, Any] = {
            "provider_name": "OpenAI",
            "timeout_seconds": timeout_seconds,
            "error_factory": _openai_error,
        }
        if opener is not None:
            kwargs["opener"] = opener
        self.client = ExternalJsonClient(API_ROOT, tokens, **kwargs)

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        safe_to_retry: bool | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            method,
            path,
            query=query,
            body=body,
            safe_to_retry=safe_to_retry,
        )

    async def ask(self, model: str, question: str, max_tokens: int) -> str:
        payload = await self.request(
            "POST",
            "/responses",
            body={
                "model": model,
                "input": question,
                "max_output_tokens": max_tokens,
                "store": False,
            },
            safe_to_retry=False,
        )
        text = _response_text(payload)
        if not text:
            raise RuntimeError("OpenAI returned no text")
        if len(text) > MAX_ANSWER_CHARACTERS:
            raise RuntimeError("OpenAI returned an answer larger than the safety limit")
        return text

    async def health(self) -> dict[str, Any]:
        try:
            payload = await self.request("GET", "/models")
            if not isinstance(payload.get("data"), list):
                raise ValueError("OpenAI returned an invalid model list")
            return {"healthy": True}
        except (OpenAIError, RuntimeError, ValueError) as error:
            message = error.reason if isinstance(error, OpenAIError) else str(error)
            return {"healthy": False, "message": message[:200]}


class OpenAIConnectionService(ApiTokenConnectionService):
    provider_id = "openai"

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(
            vault,
            provider_id=self.provider_id,
            token_secret_name=OPENAI_API_KEY_SECRET,
            legacy_token_secret_names=OPENAI_LEGACY_SECRET_NAMES,
            display_name="OpenAI",
        )
        self.adapter = OpenAIAdapter(ApiTokenProvider(self), opener=opener)

    async def probe(self) -> dict[str, Any]:
        return await self.adapter.health()


def _response_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "output_text"
                and isinstance(block.get("text"), str)
            ):
                parts.append(block["text"])
    return "\n".join(parts).strip()
