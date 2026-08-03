from __future__ import annotations

import asyncio
import io
import json
import urllib.parse
import urllib.request

import pytest

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.contracts import PermissionContext, RiskLevel, SkillCall
from assistant.integrations.microsoft_graph import (
    MICROSOFT_GRAPH_BASE_SCOPES,
    ONEDRIVE_READ_SCOPE,
    OUTLOOK_CALENDAR_READ_SCOPE,
    OUTLOOK_MAIL_READ_SCOPE,
    MicrosoftGraphConnectionService,
    MicrosoftGraphTokenManager,
)
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.onedrive import OneDriveInput, OneDriveSkill
from assistant.skills.outlook_calendar import OutlookCalendarInput, OutlookCalendarSkill
from assistant.skills.outlook_mail import OutlookMailInput, OutlookMailSkill


class MemoryVault:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class RecordingAdapter:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    async def request(self, method, path, *, query=None, body=None):
        self.requests.append({"method": method, "path": path, "query": query, "body": body})
        return self.responses.pop(0) if self.responses else {}

    async def health(self):
        return {"healthy": True}


def call(skill_id, arguments):
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


@pytest.mark.parametrize(
    ("text", "skill_id", "operation"),
    [
        ("find my Outlook mail for quarterly planning", "integration.outlook_mail", "search_messages"),
        ("show my Outlook calendar", "integration.outlook_calendar", "list_events"),
        ("find planning in my Outlook calendar", "integration.outlook_calendar", "search_events"),
        ("find my OneDrive for budget", "integration.onedrive", "search"),
        ("list my OneDrive files", "integration.onedrive", "list_children"),
    ],
)
def test_explicit_microsoft_requests_route_without_the_llm(text, skill_id, operation):
    route = DeterministicRouter().route(text)

    assert route.needs_llm is False
    assert route.intent.name == skill_id
    assert route.intent.arguments["operation"] == operation


@pytest.mark.parametrize(
    ("skill_id", "text", "message"),
    [
        ("integration.outlook_mail", "find my Outlook mail for planning", "no Microsoft account is connected"),
        ("integration.outlook_calendar", "show my Outlook calendar", "no Microsoft account is connected"),
        ("integration.onedrive", "find my OneDrive for planning", "no Microsoft account is connected"),
    ],
)
def test_microsoft_request_explains_disconnected_extension(skill_id, text, message):
    route = DeterministicRouter(skill_status=lambda _skill_id: "disconnected").route(text)

    assert route.intent.name == "conversation.answer"
    assert message in route.intent.arguments["text"]


def test_microsoft_inputs_require_explicit_read_targets():
    with pytest.raises(ValueError, match="requires query"):
        OutlookMailInput(operation="search_messages")
    with pytest.raises(ValueError, match="requires event_id"):
        OutlookCalendarInput(operation="get_event")
    with pytest.raises(ValueError, match="requires query"):
        OneDriveInput(operation="search")
    with pytest.raises(ValueError, match="provided together"):
        OutlookCalendarInput(operation="list_events", time_min="2026-08-02T00:00:00Z")


def test_microsoft_permissions_are_exact_targeted_and_read_only():
    mail = OutlookMailSkill(RecordingAdapter())
    calendar = OutlookCalendarSkill(RecordingAdapter())
    drive = OneDriveSkill(RecordingAdapter())

    mail_call = call("integration.outlook_mail", {"operation": "get_message", "message_id": "m-1"})
    calendar_call = call("integration.outlook_calendar", {"operation": "get_event", "event_id": "e-1"})
    drive_call = call("integration.onedrive", {"operation": "get_metadata", "file_id": "f-1"})

    assert mail.required_permissions(mail_call)[0].targets == ("outlook:message:m-1",)
    assert calendar.required_permissions(calendar_call)[0].targets == ("outlook-calendar:event:e-1",)
    assert drive.required_permissions(drive_call)[0].targets == ("onedrive:file:f-1",)
    assert mail.risk_for(mail_call) == RiskLevel.LOW
    assert calendar.risk_for(calendar_call) == RiskLevel.LOW
    assert drive.risk_for(drive_call) == RiskLevel.LOW


@pytest.mark.asyncio
async def test_outlook_mail_search_escapes_filter_and_returns_sensitive_items():
    adapter = RecordingAdapter(
        [
            {
                "value": [
                    {
                        "id": "m-1",
                        "subject": "Quarterly plan",
                        "bodyPreview": "Private preview",
                        "isRead": False,
                    }
                ],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=continuation"
                ),
            }
        ]
    )
    skill = OutlookMailSkill(adapter)
    result = await skill.execute(
        call(
            "integration.outlook_mail",
            {"operation": "search_messages", "query": "O'Reilly", "unread_only": True},
        ),
        asyncio.Event(),
    )

    request = adapter.requests[0]
    assert request["path"] == "/me/mailFolders/inbox/messages"
    assert request["query"]["$filter"] == "(contains(subject,'O''Reilly') or contains(bodyPreview,'O''Reilly')) and isRead eq false"
    assert result.data["next_page_token"] == "continuation"
    assert result.items[0].title == "Quarterly plan"
    assert result.items[0].sensitive is True


@pytest.mark.asyncio
async def test_outlook_calendar_time_window_and_onedrive_paths_are_fixed():
    calendar_adapter = RecordingAdapter([{"value": [{"id": "e-1", "subject": "Planning"}]}])
    await OutlookCalendarSkill(calendar_adapter).execute(
        call(
            "integration.outlook_calendar",
            {
                "operation": "list_events",
                "time_min": "2026-08-02T00:00:00Z",
                "time_max": "2026-08-03T00:00:00Z",
            },
        ),
        asyncio.Event(),
    )
    assert calendar_adapter.requests[0]["path"] == "/me/calendarView"
    assert calendar_adapter.requests[0]["query"]["startDateTime"] == "2026-08-02T00:00:00Z"

    drive_adapter = RecordingAdapter([{"value": [{"id": "f-1", "name": "Budget"}]}])
    await OneDriveSkill(drive_adapter).execute(
        call("integration.onedrive", {"operation": "search", "query": "O'Reilly plan"}),
        asyncio.Event(),
    )
    assert drive_adapter.requests[0]["path"] == "/me/drive/root/search(q='O%27%27Reilly%20plan')"

    await OneDriveSkill(drive_adapter).execute(
        call("integration.onedrive", {"operation": "list_children", "file_id": "root"}),
        asyncio.Event(),
    )
    assert drive_adapter.requests[1]["path"] == "/me/drive/root/children"


@pytest.mark.asyncio
async def test_microsoft_graph_token_manager_uses_separate_encrypted_names():
    vault = MemoryVault(
        {
            "microsoft_graph_client_id": "client",
            "microsoft_graph_refresh_token": "refresh",
        }
    )
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return Response({"access_token": "new-access", "expires_in": 3600})

    manager = MicrosoftGraphTokenManager(vault, opener=opener, clock=lambda: 1000)

    assert await manager.access_token() == "new-access"
    assert vault.get("microsoft_graph_access_token") == "new-access"
    assert "gmail_refresh_token" not in vault.values
    assert b"refresh_token=refresh" in requests[0][0].data


@pytest.mark.asyncio
async def test_microsoft_connection_requests_incremental_scope_and_disconnects():
    vault = MemoryVault()
    requests = []

    def opener(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/token"):
            return Response(
                {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
            )
        if "/me?" in request.full_url:
            return Response({"mail": "person@example.com", "displayName": "Person"})
        raise AssertionError(request.full_url)

    authorization = {}

    def browser_open(url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        authorization.update(query)
        callback = query["redirect_uri"][0] + "?" + urllib.parse.urlencode(
            {"state": query["state"][0], "code": "authorization-code"}
        )
        with urllib.request.urlopen(callback, timeout=5):
            pass
        return True

    service = MicrosoftGraphConnectionService(vault, opener=opener, browser_open=browser_open)
    status = await service.connect(
        client_id="desktop-client",
        required_scopes=(OUTLOOK_MAIL_READ_SCOPE,),
    )

    assert status.connected is True
    assert status.account == "person@example.com"
    assert set(authorization["scope"][0].split()) == set(
        (*MICROSOFT_GRAPH_BASE_SCOPES, OUTLOOK_MAIL_READ_SCOPE)
    )
    assert vault.get("microsoft_graph_refresh_token") == "refresh"
    assert "gmail_refresh_token" not in vault.values

    disconnected = await service.disconnect()
    assert disconnected.connected is False
    assert vault.get("microsoft_graph_refresh_token") is None


@pytest.mark.asyncio
async def test_microsoft_graph_extensions_are_visible_installable_and_restartable(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        offline_lock=False,
        embedding_provider="lexical",
        llm_provider="none",
    )
    container = build_container(settings)
    extension_ids = {
        "integration.outlook_mail": OUTLOOK_MAIL_READ_SCOPE,
        "integration.outlook_calendar": OUTLOOK_CALENDAR_READ_SCOPE,
        "integration.onedrive": ONEDRIVE_READ_SCOPE,
    }
    for extension_id in extension_ids:
        assert any(item.extension_id == extension_id for item in container.extension_hub.catalog.extensions)
        await container.extension_hub.install(extension_id)

    connection = container.extension_hub.connection_service("integration.outlook_mail")
    connection.vault.set("microsoft_graph_client_id", "desktop-client")
    connection.vault.set("microsoft_graph_refresh_token", "refresh-token")
    connection.vault.set(
        "microsoft_graph_granted_scopes",
        json.dumps([*MICROSOFT_GRAPH_BASE_SCOPES, *extension_ids.values()]),
    )

    restarted = build_container(settings)
    for extension_id in extension_ids:
        assert restarted.registry.is_enabled(extension_id) is True
