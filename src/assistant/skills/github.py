from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    RequestResultItem,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.integrations.github import GitHubAdapter
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.skills.microsoft_graph_common import short_id


class GitHubInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "search_repositories",
        "list_repositories",
        "get_repository",
        "search_issues",
        "list_issues",
        "get_issue",
        "list_pull_requests",
        "latest_release",
        "create_issue",
        "add_comment",
    ]
    query: str | None = Field(default=None, min_length=1, max_length=500)
    owner: str | None = Field(default=None, min_length=1, max_length=100)
    repo: str | None = Field(default=None, min_length=1, max_length=100)
    issue_number: int | None = Field(default=None, ge=1, le=10_000_000)
    max_results: int = Field(default=25, ge=1, le=50)
    page: int = Field(default=1, ge=1, le=100)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = Field(default=None, min_length=1, max_length=65_536)

    @model_validator(mode="after")
    def _operation_has_required_fields(self) -> GitHubInput:
        if self.operation in {"search_repositories", "search_issues"} and not self.query:
            raise ValueError(f"{self.operation} requires query")
        if self.operation in {
            "get_repository",
            "list_issues",
            "get_issue",
            "list_pull_requests",
            "latest_release",
            "create_issue",
            "add_comment",
        } and (not self.owner or not self.repo):
            raise ValueError(f"{self.operation} requires owner and repo")
        if self.operation in {"get_issue", "add_comment"} and self.issue_number is None:
            raise ValueError(f"{self.operation} requires issue_number")
        if self.operation == "create_issue" and (not self.title or not self.body):
            raise ValueError("create_issue requires title and body")
        if self.operation == "add_comment" and not self.body:
            raise ValueError("add_comment requires body")
        if self.operation != "create_issue" and self.title is not None:
            raise ValueError("title is only valid for create_issue")
        if self.operation not in {"create_issue", "add_comment"} and self.body is not None:
            raise ValueError("body is only valid for issue writes")
        return self


class GitHubOutput(BaseModel):
    operation: str
    resources: list[dict[str, Any]] = Field(default_factory=list)
    next_page: int | None = None


class GitHubSkill(Skill):
    input_model = GitHubInput
    output_model = GitHubOutput

    def __init__(self, adapter: GitHubAdapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="integration.github",
            display_name="GitHub",
            description=(
                "Search GitHub repositories and issues, inspect exact repositories and issues, "
                "find pull requests, inspect releases, create one issue, or add one confirmed "
                "comment. Code pushes, workflow changes, secrets, and bulk mutations are excluded."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="integration.read",
                    targets=(),
                    reason="Read approved GitHub repository and issue information.",
                ),
                PermissionScope(
                    capability="network",
                    targets=("https://api.github.com",),
                    reason="Connect to GitHub's fixed API origin.",
                ),
            ),
            risk=RiskLevel.MEDIUM,
            safe_to_retry=False,
            background_mode=BackgroundMode.CAPABILITY_GATED,
        ).model_copy(update={"entry_point": f"{__name__}:GitHubSkill"})

    def risk_for(self, call: SkillCall) -> RiskLevel:
        return (
            RiskLevel.HIGH
            if self.validate(call.arguments).operation in {"create_issue", "add_comment"}
            else RiskLevel.LOW
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        write = args.operation in {"create_issue", "add_comment"}
        return (
            PermissionScope(
                capability="integration.write" if write else "integration.read",
                targets=(_permission_target(args),),
                reason=(
                    "Change this exact GitHub issue target."
                    if write
                    else "Allow GitHub access for this exact read target."
                ),
            ),
            PermissionScope(
                capability="network",
                targets=("https://api.github.com",),
                reason="Connect to GitHub's fixed API origin.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        if args.operation == "create_issue":
            return f"Create GitHub issue {args.title!r} in {args.owner}/{args.repo}"
        if args.operation == "add_comment":
            return (
                f"Post a comment to {args.owner}/{args.repo} issue {args.issue_number}: {args.body}"
            )
        return f"GitHub: {args.operation} on {_permission_target(args)}"

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="GitHub action cancelled.")
        payload = await self._execute(args)
        resources = _resources(payload)
        next_page = _next_page(args, payload)
        output = GitHubOutput(operation=args.operation, resources=resources, next_page=next_page)
        items = tuple(
            RequestResultItem(
                item_id=f"github-{index}-{short_id(item)}",
                title=_title(args.operation, item),
                description=_description(item),
                value={"resource": item, "owner": args.owner, "repo": args.repo},
                kind=_kind(args.operation),
                sensitive=True,
            )
            for index, item in enumerate(resources, 1)
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=_summary(args.operation, len(resources)),
            display_text=_display(args.operation, resources),
            data=output.model_dump(mode="json"),
            items=items or None,
            sensitive=True,
        )

    async def _execute(self, args: GitHubInput) -> dict[str, Any]:
        if args.operation == "search_repositories":
            return await self.adapter.request(
                "GET",
                "/search/repositories",
                query={"q": args.query, "per_page": args.max_results, "page": args.page},
            )
        if args.operation == "list_repositories":
            user = await self.adapter.request("GET", "/user")
            login = user.get("login")
            if not isinstance(login, str) or not login:
                raise ValueError("GitHub did not return the authenticated user")
            return await self.adapter.request(
                "GET",
                "/search/repositories",
                query={
                    "q": f"user:{login}",
                    "per_page": args.max_results,
                    "page": args.page,
                },
            )
        repo_path = (
            f"/repos/{urllib.parse.quote(args.owner or '', safe='')}"
            f"/{urllib.parse.quote(args.repo or '', safe='')}"
        )
        if args.operation == "create_issue":
            return await self.adapter.request(
                "POST", f"{repo_path}/issues", body={"title": args.title, "body": args.body}
            )
        if args.operation == "add_comment":
            return await self.adapter.request(
                "POST",
                f"{repo_path}/issues/{args.issue_number}/comments",
                body={"body": args.body},
            )
        if args.operation == "get_repository":
            return await self.adapter.request("GET", repo_path)
        if args.operation == "latest_release":
            return await self.adapter.request("GET", f"{repo_path}/releases/latest")
        if args.operation == "get_issue":
            return await self.adapter.request("GET", f"{repo_path}/issues/{args.issue_number}")
        if args.operation == "list_issues":
            query = f"repo:{args.owner}/{args.repo} is:issue"
        elif args.operation == "list_pull_requests":
            query = f"repo:{args.owner}/{args.repo} is:pr"
        else:
            query = args.query
        return await self.adapter.request(
            "GET",
            "/search/issues",
            query={"q": query, "per_page": args.max_results, "page": args.page},
        )


def _resources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return [payload] if payload else []


def _next_page(args: GitHubInput, payload: dict[str, Any]) -> int | None:
    total = payload.get("total_count")
    if isinstance(total, int) and args.page * args.max_results < total:
        return args.page + 1
    return None


def _permission_target(args: GitHubInput) -> str:
    if args.operation in {"search_repositories", "search_issues"}:
        return f"github:query:{args.query[:200] if args.query else '*'}"
    if args.operation == "list_repositories":
        return "github:repositories:authenticated"
    repository = f"{args.owner}/{args.repo}"
    if args.operation == "create_issue":
        return f"github:create-issue:{repository}"
    if args.operation == "add_comment":
        return f"github:add-comment:{repository}:{args.issue_number}"
    if args.operation == "get_issue":
        return f"github:issue:{repository}:{args.issue_number}"
    return f"github:{args.operation}:{repository}"


def _title(operation: str, item: dict[str, Any]) -> str:
    if operation in {"search_repositories", "list_repositories", "get_repository"}:
        return str(item.get("full_name") or item.get("name") or "GitHub repository")[:500]
    if operation == "latest_release":
        return str(item.get("name") or item.get("tag_name") or "GitHub release")[:500]
    return str(item.get("title") or item.get("number") or "GitHub issue")[:500]


def _description(item: dict[str, Any]) -> str | None:
    values = [item.get("description"), item.get("state"), item.get("html_url")]
    return " • ".join(str(value)[:500] for value in values if value)[:2000] or None


def _kind(operation: str) -> str:
    if operation in {"search_repositories", "list_repositories", "get_repository"}:
        return "github_repository"
    if operation == "latest_release":
        return "github_release"
    return "github_issue"


def _summary(operation: str, count: int) -> str:
    if count:
        return f"I found {count} GitHub result(s)."
    return "I couldn't find any GitHub results."


def _display(operation: str, resources: list[dict[str, Any]]) -> str:
    if not resources:
        return "I couldn't find any GitHub results."
    lines = [f"- {_title(operation, item)}" for item in resources[:50]]
    return "\n".join(["GitHub results:", *lines])
