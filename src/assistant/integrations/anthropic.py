from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant.integrations.api_token import ApiTokenConnectionService, ApiTokenProvider
from assistant.integrations.http import ExternalJsonClient
from assistant.storage.secrets import SecretVault

API_ROOT = "https://api.anthropic.com/v1"
ANTHROPIC_API_KEY_SECRET = "anthropic_api_key"
ANTHROPIC_LEGACY_SECRET_NAMES = ("anthropic", "anthropic_api_token")
ANTHROPIC_VERSION = "2023-06-01"
MAX_ANSWER_CHARACTERS = 100_000


class AnthropicError(RuntimeError):
    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"Anthropic request failed ({status}): {self.reason}")


def _anthropic_error(status: int, reason: str, recoverable: bool) -> AnthropicError:
    return AnthropicError(status, reason, recoverable=recoverable)


class AnthropicAdapter:
    """Bounded Anthropic Messages API adapter for the reviewed extension."""

    def __init__(
        self,
        tokens: ApiTokenProvider,
        *,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        kwargs: dict[str, Any] = {
            "provider_name": "Anthropic",
            "token_header": "x-api-key",
            "token_prefix": "",
            "timeout_seconds": timeout_seconds,
            "error_factory": _anthropic_error,
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
            headers={"anthropic-version": ANTHROPIC_VERSION},
            safe_to_retry=safe_to_retry,
        )

    async def ask(self, model: str, question: str, max_tokens: int) -> str:
        payload = await self.request(
            "POST",
            "/messages",
            body={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": question}],
            },
            safe_to_retry=False,
        )
        text = _response_text(payload)
        if not text:
            raise RuntimeError("Anthropic returned no text")
        if len(text) > MAX_ANSWER_CHARACTERS:
            raise RuntimeError("Anthropic returned an answer larger than the safety limit")
        return text

    async def health(self) -> dict[str, Any]:
        try:
            payload = await self.request("GET", "/models")
            if not isinstance(payload.get("data"), list):
                raise ValueError("Anthropic returned an invalid model list")
            return {"healthy": True}
        except (AnthropicError, RuntimeError, ValueError) as error:
            message = error.reason if isinstance(error, AnthropicError) else str(error)
            return {"healthy": False, "message": message[:200]}


class AnthropicConnectionService(ApiTokenConnectionService):
    provider_id = "anthropic"

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(
            vault,
            provider_id=self.provider_id,
            token_secret_name=ANTHROPIC_API_KEY_SECRET,
            legacy_token_secret_names=ANTHROPIC_LEGACY_SECRET_NAMES,
            display_name="Anthropic",
        )
        self.adapter = AnthropicAdapter(ApiTokenProvider(self), opener=opener)

    async def probe(self) -> dict[str, Any]:
        return await self.adapter.health()


def _response_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ).strip()
