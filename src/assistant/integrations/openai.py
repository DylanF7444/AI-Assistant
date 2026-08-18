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

API_ROOT = "https://api.openai.com/v1"
OPENAI_API_KEY_SECRET = "openai_api_key"
OPENAI_LEGACY_SECRET_NAMES = ("openai", "openai_api_token")
MAX_ANSWER_CHARACTERS = 100_000


class OpenAIError(CloudProviderError):
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
            "OpenAI",
            resolved_code,  # type: ignore[arg-type]
            reason,
            status=status,
            recoverable=recoverable,
        )


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
        pricing: CloudPricing | None = None,
        offline_lock: bool = False,
    ) -> None:
        kwargs: dict[str, Any] = {
            "provider_name": "OpenAI",
            "timeout_seconds": timeout_seconds,
            "error_factory": _openai_error,
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
            raise CloudOfflineError("OpenAI")
        try:
            return await self.client.request(
                method,
                path,
                query=query,
                body=body,
                safe_to_retry=safe_to_retry,
                cancel_event=cancel_event,
            )
        except ExternalRequestCancelled:
            raise CloudRequestCancelled("OpenAI") from None
        except OpenAIError:
            raise
        except CloudProviderError:
            raise
        except RuntimeError as error:
            message = str(error)
            code = (
                "not_connected" if "not connected" in message.casefold() else "provider_unavailable"
            )
            raise OpenAIError(
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
            validate_cloud_request("openai", model, question, max_tokens)
        except (TypeError, ValueError) as error:
            raise OpenAIError(400, str(error), code="invalid_request") from None
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
            cancel_event=cancel_event,
        )
        text = _response_text(payload)
        if not text:
            raise OpenAIError(502, "OpenAI returned no text", code="invalid_response")
        if len(text) > MAX_ANSWER_CHARACTERS:
            raise OpenAIError(
                502,
                "OpenAI returned an answer larger than the safety limit",
                code="invalid_response",
            )
        return CloudAnswer(
            text=text,
            usage=usage_from_payload("openai", model, payload, pricing=self.pricing),
            request_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
        )

    async def health(self) -> dict[str, Any]:
        if self.offline_lock:
            error = CloudOfflineError("OpenAI")
            return {
                "healthy": False,
                "provider": "openai",
                "code": error.code,
                "message": error.user_message,
                "offline_lock": True,
            }
        try:
            payload = await self.request("GET", "/models")
            if not isinstance(payload.get("data"), list):
                raise ValueError("OpenAI returned an invalid model list")
            return {"healthy": True}
        except CloudRequestCancelled:
            error = CloudRequestCancelled("OpenAI")
            return {"healthy": False, "code": error.code, "message": error.user_message}
        except OpenAIError as error:
            return {
                "healthy": False,
                "provider": "openai",
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
                "provider": "openai",
                "code": code,
                "message": OpenAIError(503, str(error), code=code).user_message,
            }
        except ValueError as error:
            return {
                "healthy": False,
                "provider": "openai",
                "code": "invalid_response",
                "message": str(error)[:200],
            }


class OpenAIConnectionService(ApiTokenConnectionService):
    provider_id = "openai"

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
            token_secret_name=OPENAI_API_KEY_SECRET,
            legacy_token_secret_names=OPENAI_LEGACY_SECRET_NAMES,
            display_name="OpenAI",
        )
        self.adapter = OpenAIAdapter(
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
            kwargs["message"] = "OpenAI is blocked by Offline lock."
        return super().status(**kwargs)

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
