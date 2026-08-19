from __future__ import annotations

from datetime import datetime
from email.utils import parseaddr
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant.command_center.contracts import (
    DeliveryMode,
    DeliveryPayload,
    DestinationDefinition,
    DestinationExecutorKind,
    SourceDefinition,
    SourceUseMode,
)
from assistant.command_center.registry import (
    CommandCenterRegistry,
    DestinationContribution,
    SourceContribution,
)


class EmptyConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=20, ge=1, le=50)


class GmailUnreadConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_results: int = Field(default=20, ge=1, le=50)


class CalendarConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar_id: str = Field(default="primary", min_length=1, max_length=1024)
    query: str | None = Field(default=None, max_length=500)
    time_min: datetime | None = None
    time_max: datetime | None = None
    max_results: int = Field(default=25, ge=1, le=50)

    @model_validator(mode="after")
    def _time_range_is_ordered(self) -> CalendarConfiguration:
        if bool(self.time_min) != bool(self.time_max):
            raise ValueError("time_min and time_max must be supplied together")
        if self.time_min and self.time_max and self.time_min >= self.time_max:
            raise ValueError("time_min must be earlier than time_max")
        return self


class OutlookMailConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_id: str = Field(default="inbox", min_length=1, max_length=256)
    max_results: int = Field(default=20, ge=1, le=50)


class OutlookCalendarConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(default=None, min_length=1, max_length=500)
    time_min: str | None = Field(default=None, min_length=1, max_length=80)
    time_max: str | None = Field(default=None, min_length=1, max_length=80)
    max_results: int = Field(default=25, ge=1, le=50)

    @model_validator(mode="after")
    def _time_range_is_complete(self) -> OutlookCalendarConfiguration:
        if bool(self.time_min) != bool(self.time_max):
            raise ValueError("time_min and time_max must be supplied together")
        return self


class SlackHistoryConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1, max_length=128)
    max_results: int = Field(default=25, ge=1, le=50)


class NotionResourceConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str = Field(min_length=1, max_length=128)


class NotionSearchConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    page_size: int = Field(default=25, ge=1, le=100)


class RepositoryConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)
    max_results: int = Field(default=25, ge=1, le=50)


class DriveSearchConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=25, ge=1, le=100)


class WebPageConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=2000)
    max_characters: int = Field(default=12_000, ge=500, le=50_000)

    @field_validator("url")
    @classmethod
    def _url_is_public_https_shape(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Website sources require HTTPS without credentials")
        return value


class FileSearchConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, max_length=32_768)
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)


class NotificationDestinationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="Local Assistant", min_length=1, max_length=100)


class ExportDestinationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=32_768)
    format: str = Field(default="markdown", pattern=r"^(markdown|text)$")


class EmailDestinationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipients: tuple[str, ...] = Field(min_length=1, max_length=20)
    cc: tuple[str, ...] = Field(default=(), max_length=20)
    bcc: tuple[str, ...] = Field(default=(), max_length=20)
    subject_prefix: str = Field(default="", max_length=200)

    @field_validator("recipients", "cc", "bcc")
    @classmethod
    def _addresses_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _name, address = parseaddr(value)
            if not address or "@" not in address or len(value) > 500:
                raise ValueError("Email destinations require bounded email addresses")
        return values


class SlackDestinationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1, max_length=128)
    thread_ts: str | None = Field(default=None, min_length=1, max_length=80)


class NotionDestinationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str = Field(min_length=1, max_length=128)


class GitHubIssueDestinationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)


class GitHubCommentDestinationConfiguration(GitHubIssueDestinationConfiguration):
    issue_number: int = Field(ge=1, le=10_000_000)


def _gmail_unread(config: BaseModel) -> dict[str, Any]:
    values = GmailUnreadConfiguration.model_validate(config)
    return {"query": "in:inbox is:unread", "max_results": values.max_results}


def _outlook_unread(config: BaseModel) -> dict[str, Any]:
    values = OutlookMailConfiguration.model_validate(config)
    return {
        "folder_id": values.folder_id,
        "max_results": values.max_results,
        "unread_only": True,
    }


def _repository_issues(config: BaseModel) -> dict[str, Any]:
    values = RepositoryConfiguration.model_validate(config)
    return {
        "owner": values.owner,
        "repo": values.repo,
        "max_results": values.max_results,
    }


def _task_list(config: BaseModel) -> dict[str, Any]:
    EmptyConfiguration.model_validate(config)
    return {}


def _payload(value: DeliveryPayload | None) -> DeliveryPayload:
    if value is None:
        raise ValueError("Destination delivery requires reviewed content")
    return value


def _gmail_draft(config: BaseModel, payload: DeliveryPayload | None) -> dict[str, Any]:
    target = EmailDestinationConfiguration.model_validate(config)
    content = _payload(payload)
    return {
        "to": target.recipients,
        "cc": target.cc,
        "bcc": target.bcc,
        "subject": f"{target.subject_prefix}{content.title}",
        "body": content.body,
    }


def _outlook_draft(config: BaseModel, payload: DeliveryPayload | None) -> dict[str, Any]:
    target = EmailDestinationConfiguration.model_validate(config)
    content = _payload(payload)
    if target.cc or target.bcc:
        raise ValueError("Outlook draft destinations do not yet support CC or BCC")
    return {
        "recipients": list(target.recipients),
        "subject": f"{target.subject_prefix}{content.title}",
        "body": content.body,
    }


def _slack_post(config: BaseModel, payload: DeliveryPayload | None) -> dict[str, Any]:
    target = SlackDestinationConfiguration.model_validate(config)
    content = _payload(payload)
    return {
        "channel_id": target.channel_id,
        "thread_ts": target.thread_ts,
        "text": content.body,
    }


def _notion_create(config: BaseModel, payload: DeliveryPayload | None) -> dict[str, Any]:
    target = NotionDestinationConfiguration.model_validate(config)
    content = _payload(payload)
    return {"resource_id": target.resource_id, "title": content.title, "content": content.body}


def _notion_append(config: BaseModel, payload: DeliveryPayload | None) -> dict[str, Any]:
    target = NotionDestinationConfiguration.model_validate(config)
    return {"resource_id": target.resource_id, "content": _payload(payload).body}


def _github_issue(config: BaseModel, payload: DeliveryPayload | None) -> dict[str, Any]:
    target = GitHubIssueDestinationConfiguration.model_validate(config)
    content = _payload(payload)
    return {
        "owner": target.owner,
        "repo": target.repo,
        "title": content.title,
        "body": content.body,
    }


def _github_comment(config: BaseModel, payload: DeliveryPayload | None) -> dict[str, Any]:
    target = GitHubCommentDestinationConfiguration.model_validate(config)
    return {
        "owner": target.owner,
        "repo": target.repo,
        "issue_number": target.issue_number,
        "body": _payload(payload).body,
    }


def _source(
    registry: CommandCenterRegistry,
    *,
    source_id: str,
    provider_id: str,
    display_name: str,
    description: str,
    skill_id: str,
    operation: str | None,
    configuration_model: type[BaseModel],
    build_arguments=None,
    requires_connection: bool = True,
    remote: bool = True,
    sensitive: bool = True,
    max_items: int = 50,
    max_characters: int = 50_000,
    observation_events: tuple[Literal["changed"], ...] = (),
    observation_category: Literal["calendar", "mail"] | None = None,
    observation_minimum_interval_seconds: int = 300,
) -> None:
    registry.register_source(
        SourceContribution(
            definition=SourceDefinition(
                source_id=source_id,
                provider_id=provider_id,
                display_name=display_name,
                description=description,
                skill_id=skill_id,
                operation=operation,
                supported_modes=(
                    SourceUseMode.ON_DEMAND,
                    SourceUseMode.BRIEFING,
                    *((SourceUseMode.OBSERVATION,) if observation_events else ()),
                ),
                requires_connection=requires_connection,
                remote=remote,
                sensitive=sensitive,
                background_eligible=True,
                observation_events=observation_events,
                observation_category=observation_category,
                observation_minimum_interval_seconds=observation_minimum_interval_seconds,
                max_items=max_items,
                max_characters=max_characters,
            ),
            configuration_model=configuration_model,
            build_arguments=build_arguments,
        )
    )


def _destination(
    registry: CommandCenterRegistry,
    *,
    destination_id: str,
    provider_id: str,
    display_name: str,
    description: str,
    skill_id: str,
    operation: str,
    configuration_model: type[BaseModel],
    build_arguments,
) -> None:
    registry.register_destination(
        DestinationContribution(
            definition=DestinationDefinition(
                destination_id=destination_id,
                provider_id=provider_id,
                display_name=display_name,
                description=description,
                executor_kind=DestinationExecutorKind.SKILL,
                skill_id=skill_id,
                operation=operation,
                supported_delivery_modes=(
                    DeliveryMode.DRAFT
                    if operation == "create_draft"
                    else DeliveryMode.CONFIRM_EACH_TIME,
                    DeliveryMode.APPROVAL_QUEUE,
                ),
                requires_connection=True,
                remote=True,
                accepts_sensitive=True,
            ),
            configuration_model=configuration_model,
            build_arguments=build_arguments,
        )
    )


def register_builtin_command_center_contributions(registry: CommandCenterRegistry) -> None:
    _source(
        registry,
        source_id="gmail.unread",
        provider_id="google",
        display_name="Unread Gmail",
        description="Read a bounded set of unread Inbox messages.",
        skill_id="integration.gmail",
        operation="list_messages",
        configuration_model=GmailUnreadConfiguration,
        build_arguments=_gmail_unread,
        observation_events=("changed",),
        observation_category="mail",
        observation_minimum_interval_seconds=60,
    )
    _source(
        registry,
        source_id="gmail.search",
        provider_id="google",
        display_name="Gmail Search",
        description="Search Gmail with one explicit bounded Gmail query.",
        skill_id="integration.gmail",
        operation="list_messages",
        configuration_model=QueryConfiguration,
        observation_events=("changed",),
        observation_category="mail",
        observation_minimum_interval_seconds=60,
    )
    _source(
        registry,
        source_id="google_calendar.events",
        provider_id="google",
        display_name="Google Calendar Events",
        description="Read bounded events from one Google calendar and optional time range.",
        skill_id="integration.google_calendar",
        operation="list_events",
        configuration_model=CalendarConfiguration,
        observation_events=("changed",),
        observation_category="calendar",
        observation_minimum_interval_seconds=300,
    )
    _source(
        registry,
        source_id="google_drive.search",
        provider_id="google",
        display_name="Google Drive Search",
        description="Search bounded Google Drive metadata with one explicit query.",
        skill_id="integration.google_drive",
        operation="search",
        configuration_model=DriveSearchConfiguration,
        max_items=100,
    )
    _source(
        registry,
        source_id="outlook_mail.unread",
        provider_id="microsoft_graph",
        display_name="Unread Outlook Mail",
        description="Read a bounded set of unread messages from one Outlook folder.",
        skill_id="integration.outlook_mail",
        operation="list_messages",
        configuration_model=OutlookMailConfiguration,
        build_arguments=_outlook_unread,
    )
    _source(
        registry,
        source_id="outlook_calendar.events",
        provider_id="microsoft_graph",
        display_name="Outlook Calendar Events",
        description="Read bounded Outlook calendar events and an optional time range.",
        skill_id="integration.outlook_calendar",
        operation="list_events",
        configuration_model=OutlookCalendarConfiguration,
    )
    _source(
        registry,
        source_id="onedrive.search",
        provider_id="microsoft_graph",
        display_name="OneDrive Search",
        description="Search bounded OneDrive metadata with one explicit query.",
        skill_id="integration.onedrive",
        operation="search",
        configuration_model=DriveSearchConfiguration,
        max_items=100,
    )
    _source(
        registry,
        source_id="slack.search",
        provider_id="slack",
        display_name="Slack Search",
        description="Search bounded Slack messages with one explicit query.",
        skill_id="integration.slack",
        operation="search_messages",
        configuration_model=QueryConfiguration,
    )
    _source(
        registry,
        source_id="slack.channel_history",
        provider_id="slack",
        display_name="Slack Channel History",
        description="Read bounded history from one exact Slack channel.",
        skill_id="integration.slack",
        operation="read_history",
        configuration_model=SlackHistoryConfiguration,
    )
    _source(
        registry,
        source_id="notion.search",
        provider_id="notion",
        display_name="Notion Search",
        description="Search pages and data sources shared with one Notion connection.",
        skill_id="integration.notion",
        operation="search",
        configuration_model=NotionSearchConfiguration,
    )
    _source(
        registry,
        source_id="notion.page",
        provider_id="notion",
        display_name="Notion Page",
        description="Read one exact Notion page's bounded metadata.",
        skill_id="integration.notion",
        operation="get_page",
        configuration_model=NotionResourceConfiguration,
        max_items=1,
    )
    _source(
        registry,
        source_id="github.repository_issues",
        provider_id="github",
        display_name="GitHub Repository Issues",
        description="Read bounded issues from one exact GitHub repository.",
        skill_id="integration.github",
        operation="list_issues",
        configuration_model=RepositoryConfiguration,
        build_arguments=_repository_issues,
    )
    _source(
        registry,
        source_id="web.page",
        provider_id="web",
        display_name="Web Page",
        description="Read bounded text from one explicit public HTTPS page.",
        skill_id="web.fetch",
        operation=None,
        configuration_model=WebPageConfiguration,
        requires_connection=False,
        sensitive=False,
        max_items=1,
    )
    _source(
        registry,
        source_id="local.tasks",
        provider_id="local",
        display_name="Local Tasks",
        description="Read the local assistant task list.",
        skill_id="core.tasks",
        operation="list",
        configuration_model=EmptyConfiguration,
        build_arguments=_task_list,
        requires_connection=False,
        remote=False,
        sensitive=False,
        max_items=100,
    )
    _source(
        registry,
        source_id="local.file_search",
        provider_id="local",
        display_name="Approved File Search",
        description="Search filenames under one exact approved local root.",
        skill_id="windows.file_search",
        operation=None,
        configuration_model=FileSearchConfiguration,
        requires_connection=False,
        remote=False,
        sensitive=True,
        max_items=100,
    )

    local_destinations = (
        ("local.assistant", "Assistant", "assistant", EmptyConfiguration, True),
        ("local.inbox", "Inbox", "inbox", EmptyConfiguration, True),
        (
            "local.notification",
            "Desktop Notification",
            "notification",
            NotificationDestinationConfiguration,
            False,
        ),
        ("local.export", "Local Export", "export", ExportDestinationConfiguration, True),
    )
    for destination_id, display_name, sink, configuration, accepts_sensitive in local_destinations:
        registry.register_destination(
            DestinationContribution(
                definition=DestinationDefinition(
                    destination_id=destination_id,
                    provider_id="local",
                    display_name=display_name,
                    description=f"Deliver reviewed work to the local {display_name.lower()} sink.",
                    executor_kind=DestinationExecutorKind.LOCAL,
                    local_sink=sink,
                    supported_delivery_modes=(DeliveryMode.LOCAL,),
                    accepts_sensitive=accepts_sensitive,
                ),
                configuration_model=configuration,
            )
        )

    _destination(
        registry,
        destination_id="gmail.draft",
        provider_id="google",
        display_name="Gmail Draft",
        description="Create one reviewed Gmail draft without sending it.",
        skill_id="integration.gmail",
        operation="create_draft",
        configuration_model=EmailDestinationConfiguration,
        build_arguments=_gmail_draft,
    )
    _destination(
        registry,
        destination_id="outlook.draft",
        provider_id="microsoft_graph",
        display_name="Outlook Draft",
        description="Create one reviewed Outlook draft without sending it.",
        skill_id="integration.outlook_mail",
        operation="create_draft",
        configuration_model=EmailDestinationConfiguration,
        build_arguments=_outlook_draft,
    )
    _destination(
        registry,
        destination_id="slack.post",
        provider_id="slack",
        display_name="Slack Post",
        description="Post one reviewed message to an exact Slack channel or thread.",
        skill_id="integration.slack",
        operation="post_message",
        configuration_model=SlackDestinationConfiguration,
        build_arguments=_slack_post,
    )
    _destination(
        registry,
        destination_id="notion.create_page",
        provider_id="notion",
        display_name="New Notion Page",
        description="Create one reviewed page under an exact Notion parent.",
        skill_id="integration.notion",
        operation="create_page",
        configuration_model=NotionDestinationConfiguration,
        build_arguments=_notion_create,
    )
    _destination(
        registry,
        destination_id="notion.append",
        provider_id="notion",
        display_name="Append to Notion Page",
        description="Append reviewed content to one exact Notion page.",
        skill_id="integration.notion",
        operation="append_content",
        configuration_model=NotionDestinationConfiguration,
        build_arguments=_notion_append,
    )
    _destination(
        registry,
        destination_id="github.issue",
        provider_id="github",
        display_name="GitHub Issue",
        description="Create one reviewed issue in an exact GitHub repository.",
        skill_id="integration.github",
        operation="create_issue",
        configuration_model=GitHubIssueDestinationConfiguration,
        build_arguments=_github_issue,
    )
    _destination(
        registry,
        destination_id="github.comment",
        provider_id="github",
        display_name="GitHub Comment",
        description="Add one reviewed comment to an exact GitHub issue.",
        skill_id="integration.github",
        operation="add_comment",
        configuration_model=GitHubCommentDestinationConfiguration,
        build_arguments=_github_comment,
    )
