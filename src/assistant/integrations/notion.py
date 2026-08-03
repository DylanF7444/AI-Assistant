from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant.integrations.api_token import ApiTokenConnectionService, ApiTokenProvider
from assistant.integrations.http import ExternalJsonClient
from assistant.storage.secrets import SecretVault

API_ROOT = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
NOTION_TOKEN_SECRET = "notion_api_token"
NOTION_ACCOUNT_SECRET = "notion_workspace"


class NotionError(RuntimeError):
    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"Notion request failed ({status}): {self.reason}")


def _notion_error(status: int, reason: str, recoverable: bool) -> NotionError:
    return NotionError(status, reason, recoverable=recoverable)


class NotionAdapter:
    def __init__(
        self,
        tokens: ApiTokenProvider,
        *,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        kwargs = {
            "provider_name": "Notion",
            "timeout_seconds": timeout_seconds,
            "error_factory": _notion_error,
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
            headers={"Notion-Version": NOTION_VERSION},
            safe_to_retry=safe_to_retry,
        )

    async def health(self) -> dict[str, Any]:
        try:
            payload = await self.request("GET", "/users/me")
            account = payload.get("name") or payload.get("id")
            return {"healthy": True, "account": str(account)[:320] if account else None}
        except NotionError as error:
            return {"healthy": False, "message": error.reason}


class NotionConnectionService(ApiTokenConnectionService):
    provider_id = "notion"

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(
            vault,
            provider_id=self.provider_id,
            token_secret_name=NOTION_TOKEN_SECRET,
            account_secret_name=NOTION_ACCOUNT_SECRET,
            display_name="Notion",
        )
        self.adapter = NotionAdapter(ApiTokenProvider(self), opener=opener)

    async def probe(self) -> dict[str, Any]:
        return await self.adapter.health()
