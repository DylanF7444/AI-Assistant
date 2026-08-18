from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

CloudErrorCode = Literal[
    "offline_locked",
    "not_connected",
    "not_available",
    "authentication_failed",
    "rate_limited",
    "provider_timeout",
    "provider_unavailable",
    "invalid_request",
    "invalid_response",
    "cancelled",
]

SUPPORTED_CLOUD_PROVIDERS = frozenset({"openai", "anthropic", "codex"})
MAX_CLOUD_QUESTION_CHARACTERS = 20_000
MAX_CLOUD_MODEL_CHARACTERS = 200
MAX_CLOUD_ANSWER_CHARACTERS = 100_000
MAX_CLOUD_TOKENS = 16_000


@dataclass(frozen=True)
class TokenPricing:
    """Per-million-token pricing supplied by the user or deployment config.

    Prices are deliberately not bundled with the assistant. Provider pricing
    changes over time, and an unknown price is safer than presenting a stale
    estimate as fact.
    """

    input_usd_per_million: float
    output_usd_per_million: float

    def __post_init__(self) -> None:
        for value in (self.input_usd_per_million, self.output_usd_per_million):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("Token pricing must be finite and non-negative")

    def estimate(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        if input_tokens is None and output_tokens is None:
            return None
        return round(
            (
                (input_tokens or 0) * self.input_usd_per_million
                + (output_tokens or 0) * self.output_usd_per_million
            )
            / 1_000_000,
            8,
        )


class CloudPricing:
    """Small exact-or-wildcard pricing table for cloud providers."""

    def __init__(
        self,
        entries: Mapping[tuple[str, str], TokenPricing] | None = None,
    ) -> None:
        self._entries: dict[tuple[str, str], TokenPricing] = {}
        for key, pricing in (entries or {}).items():
            if not isinstance(key, tuple) or len(key) != 2:
                raise ValueError("Cloud pricing keys must be (provider, model) tuples")
            provider, model = (str(item).strip().casefold() for item in key)
            if not provider or not model:
                raise ValueError("Cloud pricing provider and model are required")
            if provider not in SUPPORTED_CLOUD_PROVIDERS:
                raise ValueError(f"Unsupported cloud pricing provider: {provider}")
            if not isinstance(pricing, TokenPricing):
                raise TypeError("Cloud pricing values must be TokenPricing instances")
            self._entries[(provider, model)] = pricing

    @classmethod
    def from_mapping(cls, values: Mapping[str, Mapping[str, Any]]) -> CloudPricing:
        """Build a pricing table from settings JSON keys such as ``openai:gpt-5.5``."""

        entries: dict[tuple[str, str], TokenPricing] = {}
        for key, raw in values.items():
            provider, separator, model = str(key).partition(":")
            if not separator or not provider.strip() or not model.strip():
                raise ValueError("Cloud pricing keys must use provider:model format")
            if not isinstance(raw, Mapping):
                raise ValueError(f"Cloud pricing for {key} must be an object")
            expected_fields = {"input_usd_per_million", "output_usd_per_million"}
            unknown_fields = [field for field in raw if field not in expected_fields]
            if unknown_fields:
                raise ValueError(
                    f"Cloud pricing for {key} contains unsupported fields: "
                    + ", ".join(sorted(str(field) for field in unknown_fields))
                )
            try:
                input_price = raw["input_usd_per_million"]
                output_price = raw["output_usd_per_million"]
                if isinstance(input_price, bool) or isinstance(output_price, bool):
                    raise ValueError("Boolean prices are not supported")
                entries[(provider, model)] = TokenPricing(
                    float(input_price),
                    float(output_price),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"Cloud pricing for {key} needs input_usd_per_million and "
                    "output_usd_per_million"
                ) from error
        return cls(entries)

    def for_model(self, provider: str, model: str) -> TokenPricing | None:
        provider_key = provider.strip().casefold()
        model_key = model.strip().casefold()
        return self._entries.get((provider_key, model_key)) or self._entries.get(
            (provider_key, "*")
        )

    @property
    def entries(self) -> dict[tuple[str, str], TokenPricing]:
        return dict(self._entries)


@dataclass(frozen=True)
class CloudUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_usd: float | None = None
    cost_basis: Literal["configured", "unavailable"] = "unavailable"

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        total = self.total_tokens
        if total is None and (self.input_tokens is not None or self.output_tokens is not None):
            total = (self.input_tokens or 0) + (self.output_tokens or 0)
            object.__setattr__(self, "total_tokens", total)
        if self.estimated_cost_usd is not None and (
            isinstance(self.estimated_cost_usd, bool)
            or not isinstance(self.estimated_cost_usd, int | float)
            or not math.isfinite(self.estimated_cost_usd)
            or self.estimated_cost_usd < 0
        ):
            raise ValueError("Estimated cloud cost must be finite and non-negative")
        if self.cost_basis == "configured" and self.estimated_cost_usd is None:
            raise ValueError("Configured cloud cost requires an estimate")

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for field in ("input_tokens", "output_tokens", "total_tokens", "estimated_cost_usd"):
            value = getattr(self, field)
            if value is not None:
                payload[field] = value
        if payload:
            payload["cost_basis"] = self.cost_basis
        return payload


@dataclass(frozen=True)
class CloudAnswer:
    text: str
    usage: CloudUsage | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Cloud answers must contain text")
        if len(self.text) > MAX_CLOUD_ANSWER_CHARACTERS:
            raise ValueError("Cloud answers exceed the maximum allowed length")
        if any(char not in "\n\r\t" and ord(char) < 32 for char in self.text):
            raise ValueError("Cloud answers contain unsupported control characters")
        if self.usage is not None and not isinstance(self.usage, CloudUsage):
            raise TypeError("Cloud answer usage must be CloudUsage")


class CloudProviderError(RuntimeError):
    """A provider failure with a safe, stable code for routing and UI."""

    def __init__(
        self,
        provider: str,
        code: CloudErrorCode,
        reason: str = "",
        *,
        status: int = 0,
        recoverable: bool = False,
        user_message: str | None = None,
    ) -> None:
        self.provider = provider
        self.code = code
        self.status = int(status)
        self.reason = _bounded_reason(reason)
        self.recoverable = bool(recoverable)
        self.user_message = user_message or safe_cloud_message(provider, code)
        super().__init__(self._description())

    def _description(self) -> str:
        status = f" ({self.status})" if self.status else ""
        return f"{self.provider} request failed{status}: {self.reason or self.user_message}"

    @classmethod
    def from_status(
        cls,
        provider: str,
        status: int,
        reason: str,
        *,
        recoverable: bool | None = None,
    ) -> CloudProviderError:
        code = cloud_error_code_for_status(status)
        if recoverable is None:
            recoverable = code in {"rate_limited", "provider_timeout", "provider_unavailable"}
        return cls(
            provider,
            code,
            reason,
            status=status,
            recoverable=recoverable,
        )


class CloudOfflineError(CloudProviderError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            provider,
            "offline_locked",
            "Outbound network access is blocked by offline mode.",
            user_message=safe_cloud_message(provider, "offline_locked"),
        )


class CloudRequestCancelled(CloudProviderError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            provider,
            "cancelled",
            "The request was cancelled.",
            user_message="The cloud request was cancelled.",
        )


def cloud_error_code_for_status(status: int) -> CloudErrorCode:
    if status in {401, 403}:
        return "authentication_failed"
    if status == 429:
        return "rate_limited"
    if status in {408, 504}:
        return "provider_timeout"
    if 400 <= status < 500:
        return "invalid_request"
    if status >= 500 or status == 0:
        return "provider_unavailable"
    return "provider_unavailable"


def fallback_allowed(error: CloudProviderError) -> bool:
    """Return whether an explicitly named alternate provider may be tried."""

    return error.code in {
        "not_connected",
        "rate_limited",
        "provider_timeout",
        "provider_unavailable",
    }


def safe_cloud_message(provider: str, code: CloudErrorCode) -> str:
    name = provider.strip() or "Cloud provider"
    if name.casefold() == "codex" and code == "not_connected":
        return (
            "Codex is not signed in. Open Extension Hub, select OpenAI, choose "
            "Configure OpenAI / Codex, and sign in with ChatGPT (or run `codex login`)."
        )
    if name.casefold() == "codex" and code == "provider_unavailable":
        return (
            "Codex could not run. Open Extension Hub, select OpenAI, choose "
            "Configure OpenAI / Codex, and check the Codex setup."
        )
    messages = {
        "offline_locked": f"{name} is blocked by Offline lock. Turn off Offline lock to use it.",
        "not_connected": f"{name} is not connected. Connect its API key in Extension Hub.",
        "not_available": (
            f"{name} is not installed or enabled. Install and enable it in Extension Hub "
            "before using it as a fallback."
        ),
        "authentication_failed": f"{name} rejected the credentials. Reconnect the API key in Extension Hub.",
        "rate_limited": f"{name} is rate limiting requests. Try again later or use an explicitly named fallback.",
        "provider_timeout": f"{name} did not respond before the request deadline.",
        "provider_unavailable": f"{name} is temporarily unavailable.",
        "invalid_request": f"{name} rejected the request. Check the selected model and request limits.",
        "invalid_response": f"{name} returned an invalid response.",
        "cancelled": "The cloud request was cancelled.",
    }
    return messages[code]


def usage_from_payload(
    provider: str,
    model: str,
    payload: Mapping[str, Any],
    *,
    pricing: CloudPricing | None = None,
) -> CloudUsage | None:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return None

    input_tokens = _nonnegative_int(raw.get("input_tokens", raw.get("prompt_tokens")))
    output_tokens = _nonnegative_int(raw.get("output_tokens", raw.get("completion_tokens")))
    total_tokens = _nonnegative_int(raw.get("total_tokens"))
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    if total_tokens is None:
        total_tokens = (input_tokens or 0) + (output_tokens or 0)

    configured = pricing.for_model(provider, model) if pricing is not None else None
    estimate = configured.estimate(input_tokens, output_tokens) if configured else None
    return CloudUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimate,
        cost_basis="configured" if estimate is not None else "unavailable",
    )


def format_usage(usage: CloudUsage | None) -> str:
    if usage is None:
        return "Usage unavailable."
    token_parts = []
    if usage.input_tokens is not None:
        token_parts.append(f"{usage.input_tokens:,} input")
    if usage.output_tokens is not None:
        token_parts.append(f"{usage.output_tokens:,} output")
    if not token_parts and usage.total_tokens is not None:
        token_parts.append(f"{usage.total_tokens:,} total")
    token_text = " + ".join(token_parts) if token_parts else "Token count unavailable"
    if usage.estimated_cost_usd is None:
        return f"Usage: {token_text} tokens. Cost estimate unavailable."
    return f"Usage: {token_text} tokens. Estimated cost: ${usage.estimated_cost_usd:.6f}."


def validate_cloud_request(provider: str, model: str, question: str, max_tokens: int) -> None:
    if provider not in SUPPORTED_CLOUD_PROVIDERS:
        raise ValueError(f"Unsupported cloud provider: {provider}")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("A cloud model is required")
    if len(model) > MAX_CLOUD_MODEL_CHARACTERS:
        raise ValueError("The cloud model name is too long")
    if any(char not in "\n\r\t" and ord(char) < 32 for char in model):
        raise ValueError("The cloud model name contains unsupported control characters")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("A cloud question is required")
    if len(question) > MAX_CLOUD_QUESTION_CHARACTERS:
        raise ValueError("The cloud question is too long")
    if any(char not in "\n\r\t" and ord(char) < 32 for char in question):
        raise ValueError("The cloud question contains unsupported control characters")
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens < 1
        or max_tokens > MAX_CLOUD_TOKENS
    ):
        raise ValueError(f"max_tokens must be between 1 and {MAX_CLOUD_TOKENS}")


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _bounded_reason(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value)).strip()
    return normalized[:200]
