from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant.integrations.api_token import ApiTokenConnectionService, ApiTokenProvider
from assistant.integrations.http import ExternalJsonClient
from assistant.storage.secrets import SecretVault

API_ROOT = "https://slack.com/api"
SLACK_TOKEN_SECRET = "slack_api_token"
SLACK_ACCOUNT_SECRET = "slack_workspace"


class SlackError(RuntimeError):
    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"Slack request failed ({status}): {self.reason}")


def _slack_error(status: int, reason: str, recoverable: bool) -> SlackError:
    return SlackError(status, reason, recoverable=recoverable)


class SlackAdapter:
    def __init__(
        self,
        tokens: ApiTokenProvider,
        *,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        kwargs = {
            "provider_name": "Slack",
            "timeout_seconds": timeout_seconds,
            "error_factory": _slack_error,
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
    ) -> dict[str, Any]:
        payload = await self.client.request(method, path, query=query, body=body)
        if payload.get("ok") is False:
            reason = payload.get("error") or "Slack rejected the request."
            raise SlackError(403, str(reason))
        return payload

    async def health(self) -> dict[str, Any]:
        try:
            payload = await self.request("GET", "/auth.test")
            team = payload.get("team") or payload.get("team_id")
            return {"healthy": True, "account": str(team)[:320] if team else None}
        except SlackError as error:
            return {"healthy": False, "message": error.reason}


class SlackConnectionService(ApiTokenConnectionService):
    provider_id = "slack"

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(
            vault,
            provider_id=self.provider_id,
            token_secret_name=SLACK_TOKEN_SECRET,
            account_secret_name=SLACK_ACCOUNT_SECRET,
            display_name="Slack",
        )
        self.adapter = SlackAdapter(ApiTokenProvider(self), opener=opener)

    async def probe(self) -> dict[str, Any]:
        return await self.adapter.health()
