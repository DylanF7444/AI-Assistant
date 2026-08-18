from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant.contracts import ResultStatus, SkillError, SkillResult
from assistant.integrations.cloud import (
    CloudAnswer,
    CloudOfflineError,
    CloudProviderError,
    CloudRequestCancelled,
    CloudUsage,
    fallback_allowed,
    format_usage,
    validate_cloud_request,
)
from assistant.integrations.http import ExternalRequestCancelled

CLOUD_PROVIDER = Literal["openai", "anthropic", "codex"]


class CloudUsageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    cost_basis: Literal["configured", "unavailable"] = "unavailable"


class CloudAttemptOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: CLOUD_PROVIDER
    model: str = Field(min_length=1, max_length=200)
    outcome: Literal["completed", "failed"]
    error_code: str | None = Field(default=None, min_length=1, max_length=80)


@dataclass(frozen=True)
class CloudAttempt:
    provider: str
    model: str
    outcome: Literal["completed", "failed"]
    error_code: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
        }
        if self.error_code:
            payload["error_code"] = self.error_code
        return payload


@dataclass(frozen=True)
class CloudExecution:
    provider: str
    model: str
    answer: CloudAnswer
    attempts: tuple[CloudAttempt, ...] = ()
    fallback_used: bool = False


class CloudExecutionFailure(RuntimeError):
    def __init__(self, error: CloudProviderError, attempts: tuple[CloudAttempt, ...]) -> None:
        self.error = error
        self.attempts = attempts
        super().__init__(error.user_message)


async def execute_cloud_request(
    *,
    primary_provider: str,
    primary_model: str,
    question: str,
    max_tokens: int,
    clients: Mapping[str, Any],
    fallback_provider: str | None = None,
    fallback_model: str | None = None,
    default_models: Mapping[str, str] | None = None,
    cancel_event: asyncio.Event | None = None,
    offline_lock: bool = False,
    available_providers: Mapping[str, bool] | None = None,
) -> CloudExecution:
    """Execute one primary request and, only when named, an eligible fallback.

    Fallbacks are intentionally explicit. Authentication, invalid-request,
    offline, cancellation, and malformed-response failures never resend a
    prompt to another provider.
    """

    attempts: list[CloudAttempt] = []
    if offline_lock:
        raise CloudExecutionFailure(
            CloudOfflineError(_provider_label(primary_provider)),
            tuple(attempts),
        )

    try:
        validate_cloud_request(primary_provider, primary_model, question, max_tokens)
        if fallback_provider is not None:
            if fallback_provider == primary_provider:
                raise ValueError("Fallback provider must differ from the primary provider")
            resolved_fallback_model = fallback_model or (default_models or {}).get(
                fallback_provider
            )
            if not resolved_fallback_model:
                resolved_fallback_model = primary_model
            validate_cloud_request(fallback_provider, resolved_fallback_model, question, max_tokens)
    except (TypeError, ValueError) as error:
        raise CloudExecutionFailure(
            CloudProviderError(
                _provider_label(primary_provider),
                "invalid_request",
                str(error),
            ),
            tuple(attempts),
        ) from None

    targets = [(primary_provider, primary_model)]
    if fallback_provider is not None:
        resolved_model = fallback_model or (default_models or {}).get(fallback_provider)
        if not resolved_model:
            resolved_model = primary_model
        targets.append((fallback_provider, resolved_model))

    for index, (provider, model) in enumerate(targets):
        try:
            answer = await _invoke_client(
                provider,
                model,
                question,
                max_tokens,
                clients.get(provider),
                cancel_event,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            error = _as_cloud_error(provider, exception, cancel_event)
            attempts.append(CloudAttempt(provider, model, "failed", error.code))
            is_explicit_fallback = index == 0 and fallback_provider is not None
            if is_explicit_fallback and fallback_allowed(error):
                if available_providers is not None and not available_providers.get(
                    fallback_provider, False
                ):
                    availability_error = CloudProviderError(
                        _provider_label(fallback_provider),
                        "not_available",
                        "The explicitly selected fallback is not installed or enabled.",
                    )
                    attempts.append(
                        CloudAttempt(
                            fallback_provider,
                            targets[index + 1][1],
                            "failed",
                            availability_error.code,
                        )
                    )
                    raise CloudExecutionFailure(
                        availability_error,
                        tuple(attempts),
                    ) from None
                continue
            raise CloudExecutionFailure(error, tuple(attempts)) from None
        attempts.append(CloudAttempt(provider, model, "completed"))
        if cancel_event is not None and cancel_event.is_set():
            error = CloudRequestCancelled(_provider_label(provider))
            attempts[-1] = CloudAttempt(provider, model, "failed", error.code)
            raise CloudExecutionFailure(error, tuple(attempts)) from None
        return CloudExecution(
            provider=provider,
            model=model,
            answer=answer,
            attempts=tuple(attempts),
            fallback_used=index > 0,
        )

    error = CloudProviderError(
        _provider_label(primary_provider),
        "provider_unavailable",
        "All explicitly selected cloud providers failed.",
        recoverable=True,
    )
    raise CloudExecutionFailure(error, tuple(attempts))


async def _invoke_client(
    provider: str,
    model: str,
    question: str,
    max_tokens: int,
    client: Any,
    cancel_event: asyncio.Event | None,
) -> CloudAnswer:
    if cancel_event is not None and cancel_event.is_set():
        raise CloudRequestCancelled(_provider_label(provider))
    if client is None:
        code = "not_connected" if provider in {"openai", "anthropic"} else "provider_unavailable"
        raise CloudProviderError(
            _provider_label(provider),
            code,  # type: ignore[arg-type]
            "No client is configured.",
            recoverable=code == "not_connected",
        )

    method = getattr(client, "ask_with_usage", None)
    if not callable(method):
        method = getattr(client, "ask", None)
    if not callable(method):
        raise CloudProviderError(
            _provider_label(provider),
            "provider_unavailable",
            "No compatible cloud client is configured.",
            recoverable=True,
        )

    kwargs: dict[str, Any] = {}
    parameters = inspect.signature(method).parameters
    if "cancel_event" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    ):
        kwargs["cancel_event"] = cancel_event
    if provider == "codex":
        value = await method(question, model=model, **kwargs)
    else:
        value = await method(model, question, max_tokens, **kwargs)
    if isinstance(value, CloudAnswer):
        return value
    if isinstance(value, str):
        try:
            return CloudAnswer(value.strip())
        except (TypeError, ValueError) as error:
            raise CloudProviderError(
                _provider_label(provider),
                "invalid_response",
                str(error),
            ) from None
    raise CloudProviderError(
        _provider_label(provider),
        "invalid_response",
        "The cloud client returned no text.",
    )


def cloud_failure_result(
    failure: CloudExecutionFailure,
    *,
    provider: str,
    model: str,
) -> SkillResult:
    error = failure.error
    status = (
        ResultStatus.CANCELLED
        if error.code == "cancelled"
        else ResultStatus.TIMED_OUT
        if error.code == "provider_timeout"
        else ResultStatus.FAILED
    )
    return SkillResult(
        status=status,
        summary=error.user_message,
        display_text=error.user_message,
        speech_text=error.user_message,
        error=SkillError(
            code=error.code,
            message=error.user_message,
            recoverable=error.recoverable,
        ),
        data={
            "provider": provider,
            "model": model,
            "attempts": [attempt.as_dict() for attempt in failure.attempts],
            **({"fallback_used": True} if len(failure.attempts) > 1 else {}),
        },
        sensitive=True,
    )


def cloud_output_fields(
    execution: CloudExecution,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "provider": execution.provider,
        "model": execution.model,
        "answer": execution.answer.text,
    }
    if execution.answer.usage is not None:
        fields["usage"] = usage_output(execution.answer.usage).model_dump(
            mode="json",
            exclude_none=True,
        )
    if execution.attempts and (execution.fallback_used or len(execution.attempts) > 1):
        fields["attempts"] = [
            CloudAttemptOutput.model_validate(attempt.as_dict()).model_dump(
                mode="json", exclude_none=True
            )
            for attempt in execution.attempts
        ]
    if execution.fallback_used:
        fields["fallback_used"] = True
    return fields


def usage_output(usage: CloudUsage) -> CloudUsageOutput:
    return CloudUsageOutput(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        estimated_cost_usd=usage.estimated_cost_usd,
        cost_basis=usage.cost_basis,
    )


def cloud_success_summary(execution: CloudExecution) -> str:
    prefix = (
        f"Used explicit fallback {execution.provider} ({execution.model})."
        if execution.fallback_used
        else f"Answered using {execution.provider} ({execution.model})."
    )
    if execution.answer.usage is None:
        return f"{prefix} {format_usage(None)}"
    return f"{prefix} {format_usage(execution.answer.usage)}"


def _as_cloud_error(
    provider: str,
    exception: Exception,
    cancel_event: asyncio.Event | None,
) -> CloudProviderError:
    if isinstance(exception, CloudProviderError):
        return exception
    if isinstance(exception, ExternalRequestCancelled):
        return CloudRequestCancelled(_provider_label(provider))
    if cancel_event is not None and cancel_event.is_set():
        return CloudRequestCancelled(_provider_label(provider))
    text = str(exception)
    lowered = text.casefold()
    if isinstance(exception, PermissionError) or "offline mode" in lowered:
        return CloudOfflineError(_provider_label(provider))
    if "cancel" in lowered:
        return CloudRequestCancelled(_provider_label(provider))
    if provider == "codex":
        if "unknown model" in lowered or "model_not_found" in lowered:
            return CloudProviderError(
                _provider_label(provider),
                "invalid_request",
                text,
                user_message=(
                    "Codex does not recognize the configured model. Choose a supported Codex "
                    "model in Settings and try again."
                ),
            )
        if "not inside a trusted directory" in lowered or "skip-git-repo-check" in lowered:
            return CloudProviderError(
                _provider_label(provider),
                "invalid_request",
                text,
                user_message=(
                    "Codex refused the current working directory because it is not a Git "
                    "repository. Restart the assistant so it can use the safe non-repository "
                    "execution mode."
                ),
            )
        if "install it" in lowered or "on path" in lowered:
            return CloudProviderError(
                _provider_label(provider),
                "provider_unavailable",
                text,
                recoverable=True,
                user_message=(
                    "The Codex CLI is not installed or is not on PATH. Install it, then "
                    "run `codex login` with ChatGPT."
                ),
            )
        if any(
            marker in lowered
            for marker in (
                "not logged in",
                "not authenticated",
                "authentication required",
                "login required",
                "codex login",
            )
        ):
            return CloudProviderError(
                _provider_label(provider),
                "not_connected",
                text,
                recoverable=True,
            )
    if "not connected" in lowered or "api key" in lowered or "token" in lowered:
        return CloudProviderError(
            _provider_label(provider),
            "not_connected",
            text,
            recoverable=True,
        )
    if isinstance(exception, TimeoutError) or "timeout" in lowered or "deadline" in lowered:
        return CloudProviderError(
            _provider_label(provider),
            "provider_timeout",
            text,
            recoverable=True,
        )
    return CloudProviderError(
        _provider_label(provider),
        "provider_unavailable",
        text,
        recoverable=True,
    )


def _provider_label(provider: str) -> str:
    return {
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "codex": "Codex",
    }.get(provider, provider.title())
