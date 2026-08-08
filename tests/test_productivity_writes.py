from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant.contracts import PermissionContext, RiskLevel, SkillCall
from assistant.skills.github import GitHubSkill
from assistant.skills.google_drive import GoogleDriveSkill
from assistant.skills.notion import NotionSkill
from assistant.skills.onedrive import OneDriveSkill
from assistant.skills.outlook_calendar import OutlookCalendarSkill
from assistant.skills.outlook_mail import OutlookMailSkill
from assistant.skills.slack import SlackSkill


class FakeAdapter:
    def __init__(self) -> None:
        self.requests = []

    async def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if path.endswith("/send") or method == "DELETE":
            return {}
        if path == "/chat.postMessage":
            return {"ok": True, "message": {"ts": "1.2", "text": kwargs["body"]["text"]}}
        if path.endswith("/children") and method == "PATCH":
            return {"results": [{"id": "block-1", "type": "paragraph"}]}
        return {"id": "resource-1", "subject": "Created", "title": "Created"}

    async def health(self):
        return {"healthy": True}


class FakeDriveAdapter(FakeAdapter):
    async def download(self, file_id, *, max_bytes):
        assert max_bytes == 25 * 1024 * 1024
        return b"downloaded"

    async def upload(self, **kwargs):
        self.requests.append(("UPLOAD", "/files", kwargs))
        return {"id": "drive-1", "name": kwargs["name"], "size": len(kwargs["payload"])}


class FakeGraphAdapter(FakeAdapter):
    async def request(self, method, path, **kwargs):
        if kwargs.get("query", {}).get("$select") == "id,name,size,@microsoft.graph.downloadUrl":
            return {
                "id": "one-1",
                "name": "remote.txt",
                "@microsoft.graph.downloadUrl": "https://tenant.sharepoint.com/signed?token=x",
            }
        return await super().request(method, path, **kwargs)

    async def download_url(self, url, *, max_bytes):
        assert url.startswith("https://tenant.sharepoint.com/")
        return b"onedrive"

    async def upload_bytes(self, path, payload, *, content_type):
        self.requests.append(("UPLOAD", path, {"payload": payload, "content_type": content_type}))
        return {"id": "one-2", "name": Path(path).name, "size": len(payload)}


def _call(skill_id: str, arguments: dict) -> SkillCall:
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="write-test"),
    )


@pytest.mark.asyncio
async def test_outlook_draft_and_send_are_separate_exact_actions():
    adapter = FakeAdapter()
    skill = OutlookMailSkill(adapter)
    draft = _call(
        "integration.outlook_mail",
        {
            "operation": "create_draft",
            "recipients": ["rowan@example.com"],
            "subject": "Launch",
            "body": "Ready for review.",
        },
    )
    send = _call(
        "integration.outlook_mail",
        {"operation": "send_draft", "message_id": "draft-123"},
    )

    await skill.execute(draft, asyncio.Event())
    await skill.execute(send, asyncio.Event())

    assert adapter.requests[0][0:2] == ("POST", "/me/messages")
    assert adapter.requests[1][0:2] == ("POST", "/me/messages/draft-123/send")
    assert skill.required_permissions(draft)[0].capability == "integration.write"
    assert skill.required_permissions(send)[0].capability == "messaging"
    assert skill.risk_for(send) == RiskLevel.HIGH
    assert "do not send" in (await skill.preview(draft))


@pytest.mark.asyncio
async def test_outlook_calendar_mutations_are_non_retryable_and_confirmed():
    adapter = FakeAdapter()
    skill = OutlookCalendarSkill(adapter)
    create = _call(
        "integration.outlook_calendar",
        {
            "operation": "create_event",
            "subject": "Review",
            "time_min": "2026-08-10T15:00:00",
            "time_max": "2026-08-10T15:30:00",
        },
    )

    await skill.execute(create, asyncio.Event())

    assert adapter.requests[0][0:2] == ("POST", "/me/events")
    assert adapter.requests[0][2]["body"]["start"]["timeZone"] == "UTC"
    assert skill.manifest.safe_to_retry is False
    assert skill.risk_for(create) == RiskLevel.HIGH
    assert skill.required_permissions(create)[0].capability == "integration.write"


@pytest.mark.asyncio
async def test_slack_notion_and_github_writes_have_exact_targets_and_high_risk():
    slack_adapter = FakeAdapter()
    slack = SlackSkill(slack_adapter)
    slack_call = _call(
        "integration.slack",
        {"operation": "post_message", "channel_id": "C123", "text": "Ship is ready."},
    )
    notion_adapter = FakeAdapter()
    notion = NotionSkill(notion_adapter)
    notion_call = _call(
        "integration.notion",
        {
            "operation": "create_page",
            "resource_id": "parent-1",
            "title": "Launch",
            "content": "Checklist",
        },
    )
    github_adapter = FakeAdapter()
    github = GitHubSkill(github_adapter)
    github_call = _call(
        "integration.github",
        {
            "operation": "add_comment",
            "owner": "acme",
            "repo": "assistant",
            "issue_number": 7,
            "body": "Verified on Windows.",
        },
    )

    await slack.execute(slack_call, asyncio.Event())
    await notion.execute(notion_call, asyncio.Event())
    await github.execute(github_call, asyncio.Event())

    assert slack.required_permissions(slack_call)[0].capability == "messaging"
    assert slack.risk_for(slack_call) == RiskLevel.HIGH
    assert notion_adapter.requests[0][0:2] == ("POST", "/pages")
    assert notion.required_permissions(notion_call)[0].targets == ("notion:create-page:parent-1",)
    assert github_adapter.requests[0][0:2] == (
        "POST",
        "/repos/acme/assistant/issues/7/comments",
    )
    assert github.required_permissions(github_call)[0].targets == (
        "github:add-comment:acme/assistant:7",
    )


@pytest.mark.asyncio
async def test_google_drive_transfer_separates_local_and_remote_permissions(tmp_path: Path):
    adapter = FakeDriveAdapter()
    skill = GoogleDriveSkill(adapter)
    source = tmp_path / "source.txt"
    source.write_text("upload", encoding="utf-8")
    destination = tmp_path / "download.txt"
    upload = _call(
        "integration.google_drive",
        {"operation": "upload", "local_path": str(source), "parent_id": "folder-1"},
    )
    download = _call(
        "integration.google_drive",
        {"operation": "download", "file_id": "file-1", "local_path": str(destination)},
    )

    await skill.execute(upload, asyncio.Event())
    await skill.execute(download, asyncio.Event())

    assert [scope.capability for scope in skill.required_permissions(upload)] == [
        "filesystem.content.read",
        "integration.write",
        "network",
    ]
    assert [scope.capability for scope in skill.required_permissions(download)] == [
        "filesystem.write",
        "integration.read",
        "network",
    ]
    assert destination.read_bytes() == b"downloaded"
    with pytest.raises(FileExistsError):
        await skill.execute(download, asyncio.Event())


@pytest.mark.asyncio
async def test_onedrive_transfer_uses_signed_download_without_exposing_it(tmp_path: Path):
    adapter = FakeGraphAdapter()
    skill = OneDriveSkill(adapter)
    source = tmp_path / "source.txt"
    source.write_text("upload", encoding="utf-8")
    destination = tmp_path / "download.txt"
    upload = _call(
        "integration.onedrive",
        {"operation": "upload", "file_id": "folder-1", "local_path": str(source)},
    )
    download = _call(
        "integration.onedrive",
        {"operation": "download", "file_id": "file-1", "local_path": str(destination)},
    )

    await skill.execute(upload, asyncio.Event())
    result = await skill.execute(download, asyncio.Event())

    assert skill.required_permissions(upload)[1].capability == "integration.write"
    assert destination.read_bytes() == b"onedrive"
    assert "downloadUrl" not in (result.display_text or "")
    assert "downloadUrl" not in result.summary
