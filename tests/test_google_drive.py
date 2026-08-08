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
from assistant.integrations.google_drive import (
    DEFAULT_GOOGLE_DRIVE_CLIENT_ID,
    DRIVE_FILE_SCOPE,
    DRIVE_READ_SCOPE,
    GoogleDriveConnectionService,
    GoogleDriveTokenManager,
)
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.google_drive import GoogleDriveInput, GoogleDriveSkill


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


def call(arguments):
    return SkillCall(
        skill_id="integration.google_drive",
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("find my drive for quarterly planning", "search"),
        ("search quarterly planning in my Google Drive", "search"),
        ("list my Google Drive files", "list_children"),
    ],
)
def test_common_google_drive_requests_route_without_the_llm(text, operation):
    route = DeterministicRouter().route(text)

    assert route.needs_llm is False
    assert route.clarification is None
    assert route.intent.name == "integration.google_drive"
    assert route.intent.arguments["operation"] == operation
    GoogleDriveInput.model_validate(route.intent.arguments)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("disabled", "installed but disabled"),
        ("disconnected", "no account is connected"),
        ("reauthorization_required", "needs additional access"),
        ("unavailable", "not installed"),
    ],
)
def test_google_drive_request_explains_unavailable_extension_state(status, message):
    route = DeterministicRouter(
        skill_status=lambda _skill_id: status,
    ).route("find my drive for planning")

    assert route.intent.name == "conversation.answer"
    assert message in route.intent.arguments["text"]


def test_google_drive_input_requires_a_safe_explicit_target():
    with pytest.raises(ValueError, match="requires query"):
        GoogleDriveInput(operation="search")
    with pytest.raises(ValueError, match="requires file_id"):
        GoogleDriveInput(operation="get_metadata")


def test_google_drive_permissions_are_exact_targeted_and_read_only():
    skill = GoogleDriveSkill(RecordingAdapter())
    search = call({"operation": "search", "query": "quarterly planning"})
    metadata = call({"operation": "get_metadata", "file_id": "file-1"})

    assert skill.required_permissions(search)[0].capability == "integration.read"
    assert skill.required_permissions(search)[0].targets == (
        "google_drive:query:quarterly planning",
    )
    assert skill.required_permissions(metadata)[0].targets == ("google_drive:file:file-1",)
    assert skill.risk_for(search) == RiskLevel.LOW


@pytest.mark.asyncio
async def test_search_escapes_drive_query_and_returns_bounded_selectable_items():
    adapter = RecordingAdapter(
        [
            {
                "files": [
                    {
                        "id": "file-1",
                        "name": "Quarterly plan",
                        "mimeType": "text/plain",
                        "modifiedTime": "2026-08-02T12:00:00Z",
                        "description": "Private plan",
                    }
                ],
                "nextPageToken": "next",
            }
        ]
    )
    result = await GoogleDriveSkill(adapter).execute(
        call({"operation": "search", "query": "O'Reilly\\plan", "max_results": 10}),
        asyncio.Event(),
    )

    query = adapter.requests[0]["query"]
    assert query["q"] == (
        "trashed = false and (name contains 'O\\'Reilly\\\\plan' "
        "or fullText contains 'O\\'Reilly\\\\plan')"
    )
    assert query["pageSize"] == 10
    assert result.data["next_page_token"] == "next"
    assert result.items[0].title == "Quarterly plan"
    assert result.items[0].value["file_id"] == "file-1"
    assert result.items[0].sensitive is True


@pytest.mark.asyncio
async def test_list_children_and_metadata_use_fixed_drive_paths():
    adapter = RecordingAdapter(
        [
            {"files": [{"id": "child", "name": "Child"}]},
            {"id": "folder", "name": "Folder", "mimeType": "application/vnd.google-apps.folder"},
        ]
    )
    skill = GoogleDriveSkill(adapter)

    await skill.execute(call({"operation": "list_children", "file_id": "folder"}), asyncio.Event())
    await skill.execute(call({"operation": "get_metadata", "file_id": "folder"}), asyncio.Event())

    assert adapter.requests[0]["path"] == "/files"
    assert adapter.requests[0]["query"]["q"] == "'folder' in parents and trashed = false"
    assert adapter.requests[1]["path"] == "/files/folder"


@pytest.mark.asyncio
async def test_drive_token_manager_uses_separate_encrypted_names():
    vault = MemoryVault(
        {"google_drive_client_id": "client", "google_drive_refresh_token": "refresh"}
    )
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return Response({"access_token": "new-access", "expires_in": 3600})

    manager = GoogleDriveTokenManager(vault, opener=opener, clock=lambda: 1000)

    assert await manager.access_token() == "new-access"
    assert vault.get("google_drive_access_token") == "new-access"
    assert "gmail_refresh_token" not in vault.values
    assert b"refresh_token=refresh" in requests[0][0].data


@pytest.mark.asyncio
async def test_connection_service_requests_drive_scope_and_disconnects_separately():
    vault = MemoryVault()
    requests = []

    def opener(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/token"):
            return Response(
                {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
            )
        if "/about?" in request.full_url:
            return Response({"user": {"emailAddress": "person@example.com"}})
        if request.full_url.endswith("/revoke"):
            return Response({})
        raise AssertionError(request.full_url)

    authorization = {}

    def browser_open(url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        authorization.update(query)
        callback = (
            query["redirect_uri"][0]
            + "?"
            + urllib.parse.urlencode({"state": query["state"][0], "code": "authorization-code"})
        )
        with urllib.request.urlopen(callback, timeout=5):
            pass
        return True

    service = GoogleDriveConnectionService(vault, opener=opener, browser_open=browser_open)
    status = await service.connect(client_secret="desktop-secret")

    assert status.connected is True
    assert status.account == "person@example.com"
    assert authorization["scope"] == [DRIVE_READ_SCOPE]
    assert authorization["client_id"] == [DEFAULT_GOOGLE_DRIVE_CLIENT_ID]
    assert vault.get("google_drive_refresh_token") == "refresh"
    assert "gmail_refresh_token" not in vault.values

    disconnected = await service.disconnect()
    assert disconnected.connected is False
    assert vault.get("google_drive_refresh_token") is None


@pytest.mark.asyncio
async def test_google_drive_is_visible_installable_and_restartable_from_the_hub(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        offline_lock=False,
        embedding_provider="lexical",
        llm_provider="none",
    )
    container = build_container(settings)
    entry = next(
        item
        for item in container.extension_hub.catalog.extensions
        if item.extension_id == "integration.google_drive"
    )

    manifest = await container.extension_hub.install(entry.extension_id)
    connection = container.extension_hub.connection_service(entry.extension_id)
    connection.vault.set("google_drive_client_id", "desktop-client")
    connection.vault.set("google_drive_refresh_token", "refresh-token")
    connection.vault.set(
        "google_drive_granted_scopes",
        json.dumps([DRIVE_READ_SCOPE, DRIVE_FILE_SCOPE]),
    )

    assert manifest.skill_id == "integration.google_drive"
    assert container.registry.is_enabled("integration.google_drive") is False

    restarted = build_container(settings)
    assert restarted.registry.is_enabled("integration.google_drive") is True
