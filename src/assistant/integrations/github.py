from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant.integrations.api_token import ApiTokenConnectionService, ApiTokenProvider
from assistant.integrations.http import ExternalJsonClient
from assistant.storage.secrets import SecretVault

API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_TOKEN_SECRET = "github_api_token"
GITHUB_ACCOUNT_SECRET = "github_account"


class GitHubError(RuntimeError):
    def __init__(self, status: int, reason: str, *, recoverable: bool = False) -> None:
        self.status = status
        self.reason = reason[:200]
        self.recoverable = recoverable
        super().__init__(f"GitHub request failed ({status}): {self.reason}")


def _github_error(status: int, reason: str, recoverable: bool) -> GitHubError:
    return GitHubError(status, reason, recoverable=recoverable)


class GitHubAdapter:
    def __init__(
        self,
        tokens: ApiTokenProvider,
        *,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        kwargs = {
            "provider_name": "GitHub",
            "timeout_seconds": timeout_seconds,
            "error_factory": _github_error,
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
        return await self.client.request(
            method,
            path,
            query=query,
            body=body,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )

    async def health(self) -> dict[str, Any]:
        try:
            payload = await self.request("GET", "/user")
            login = payload.get("login")
            return {"healthy": True, "account": str(login)[:320] if login else None}
        except GitHubError as error:
            return {"healthy": False, "message": error.reason}


class GitHubConnectionService(ApiTokenConnectionService):
    provider_id = "github"

    def __init__(
        self,
        vault: SecretVault,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(
            vault,
            provider_id=self.provider_id,
            token_secret_name=GITHUB_TOKEN_SECRET,
            account_secret_name=GITHUB_ACCOUNT_SECRET,
            display_name="GitHub",
        )
        self.adapter = GitHubAdapter(ApiTokenProvider(self), opener=opener)

    async def probe(self) -> dict[str, Any]:
        return await self.adapter.health()
