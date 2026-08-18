from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant.integrations.api_token import ApiTokenConnectionService, ApiTokenProvider
from assistant.integrations.cloud import (
    CloudAnswer,
    CloudOfflineError,
    CloudPricing,
    CloudProviderError,
    CloudRequestCancelled,
    cloud_error_code_for_status,
    usage_from_payload,
    validate_cloud_request,
)
from assistant.integrations.connections import ConnectionStatus
from assistant.integrations.http import ExternalJsonClient, ExternalRequestCancelled
from assistant.storage.secrets import SecretVault

API_ROOT = "https://api.anthropic.com/v1"
ANTHROPIC_API_KEY_SECRET = "anthropic_api_key"
ANTHROPIC_LEGACY_SECRET_NAMES = ("anthropic", "anthropic_api_token")
ANTHROPIC_VERSION = "2023-06-01"
MAX_ANSWER_CHARACTERS = 100_000


class AnthropicError(CloudProviderError):
    def __init__(
        self,
        status: int,
        reason: str,
        *,
        recoverable: bool = False,
        code: str | None = None,
    ) -> None:
        resolved_code = code or (
            "offline_locked"
            if "offline mode" in reason.casefold()
            else "not_connected"
            if "not connected" in reason.casefold()
            else cloud_error_code_for_status(status)
        )
        super().__init__(
            "Anthropic",
            resolved_code,  # type: ignore[arg-type]
            reason,
            status=status,
            recoverable=recoverable,
        )


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
        pricing: CloudPricing | None = None,
        offline_lock: bool = False,
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
        self.pricing = pricing
        self.offline_lock = bool(offline_lock)

    def set_offline_lock(self, enabled: bool) -> None:
        self.offline_lock = bool(enabled)

    def set_pricing(self, pricing: CloudPricing | None) -> None:
        self.pricing = pricing

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        safe_to_retry: bool | None = None,
        cancel_event=None,
    ) -> dict[str, Any]:
        if self.offline_lock:
            raise CloudOfflineError("Anthropic")
        try:
            return await self.client.request(
                method,
                path,
                query=query,
                body=body,
                headers={"anthropic-version": ANTHROPIC_VERSION},
                safe_to_retry=safe_to_retry,
                cancel_event=cancel_event,
            )
        except ExternalRequestCancelled:
            raise CloudRequestCancelled("Anthropic") from None
        except AnthropicError:
            raise
        except CloudProviderError:
            raise
        except RuntimeError as error:
            message = str(error)
            code = (
                "not_connected" if "not connected" in message.casefold() else "provider_unavailable"
            )
            raise AnthropicError(
                503,
                message,
                recoverable=True,
                code=code,
            ) from error

    async def ask(self, model: str, question: str, max_tokens: int, *, cancel_event=None) -> str:
        answer = await self.ask_with_usage(
            model,
            question,
            max_tokens,
            cancel_event=cancel_event,
        )
        return answer.text

    async def ask_with_usage(
        self,
        model: str,
        question: str,
        max_tokens: int,
        *,
        cancel_event=None,
    ) -> CloudAnswer:
        try:
            validate_cloud_request("anthropic", model, question, max_tokens)
        except (TypeError, ValueError) as error:
            raise AnthropicError(400, str(error), code="invalid_request") from None
        payload = await self.request(
            "POST",
            "/messages",
            body={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": question}],
            },
            safe_to_retry=False,
            cancel_event=cancel_event,
        )
        text = _response_text(payload)
        if not text:
            raise AnthropicError(502, "Anthropic returned no text", code="invalid_response")
        if len(text) > MAX_ANSWER_CHARACTERS:
            raise AnthropicError(
                502,
                "Anthropic returned an answer larger than the safety limit",
                code="invalid_response",
            )
        return CloudAnswer(
            text=text,
            usage=usage_from_payload("anthropic", model, payload, pricing=self.pricing),
            request_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
        )

    async def health(self) -> dict[str, Any]:
        if self.offline_lock:
            error = CloudOfflineError("Anthropic")
            return {
                "healthy": False,
                "provider": "anthropic",
                "code": error.code,
                "message": error.user_message,
                "offline_lock": True,
            }
        try:
            payload = await self.request("GET", "/models")
            if not isinstance(payload.get("data"), list):
                raise ValueError("Anthropic returned an invalid model list")
            return {"healthy": True}
        except CloudRequestCancelled:
            error = CloudRequestCancelled("Anthropic")
            return {"healthy": False, "code": error.code, "message": error.user_message}
        except AnthropicError as error:
            return {
                "healthy": False,
                "provider": "anthropic",
                "code": error.code,
                "message": error.user_message,
            }
        except RuntimeError as error:
            code = (
                "not_connected"
                if "not connected" in str(error).casefold()
                else "provider_unavailable"
            )
            return {
                "healthy": False,
                "provider": "anthropic",
                "code": code,
                "message": AnthropicError(503, str(error), code=code).user_message,
            }
        except ValueError as error:
            return {
                "healthy": False,
                "provider": "anthropic",
                "code": "invalid_response",
                "message": str(error)[:200],
            }


class AnthropicConnectionService(ApiTokenConnectionService):
    provider_id = "anthropic"

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] | None = None,
        pricing: CloudPricing | None = None,
        offline_lock: bool = False,
    ) -> None:
        super().__init__(
            vault,
            provider_id=self.provider_id,
            token_secret_name=ANTHROPIC_API_KEY_SECRET,
            legacy_token_secret_names=ANTHROPIC_LEGACY_SECRET_NAMES,
            display_name="Anthropic",
        )
        self.adapter = AnthropicAdapter(
            ApiTokenProvider(self),
            opener=opener,
            pricing=pricing,
            offline_lock=offline_lock,
        )

    def set_offline_lock(self, enabled: bool) -> None:
        self.adapter.set_offline_lock(enabled)

    def set_pricing(self, pricing: CloudPricing | None) -> None:
        self.adapter.set_pricing(pricing)

    def status(self, **kwargs: Any) -> ConnectionStatus:
        if self.adapter.offline_lock:
            kwargs["healthy"] = False
            kwargs["message"] = "Anthropic is blocked by Offline lock."
        return super().status(**kwargs)

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
