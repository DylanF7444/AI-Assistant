from __future__ import annotations

import asyncio
import base64
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from email import policy
from email.parser import BytesParser

import pytest

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.contracts import PermissionContext, RiskLevel, SkillCall
from assistant.integrations.gmail import (
    DEFAULT_GMAIL_CLIENT_ID,
    GMAIL_SCOPE,
    GmailAdapter,
    GmailConnectionService,
    GmailTokenManager,
)
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.gmail import GmailInput, GmailSkill


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
        skill_id="integration.gmail",
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("check my gmail", "list_messages"),
        ("show my inbox", "list_messages"),
        ("show me my unread emails", "list_messages"),
        ("find my gmail for quarterly planning", "list_messages"),
        ("find email from person@example.com", "list_messages"),
        (
            "send an email to person@example.com subject Status saying All done",
            "send_message",
        ),
        (
            "draft an email to person@example.com subject Status saying All done",
            "create_draft",
        ),
    ],
)
def test_common_gmail_requests_route_without_the_llm(text, operation):
    route = DeterministicRouter().route(text)

    assert route.needs_llm is False
    assert route.clarification is None
    assert route.intent.name == "integration.gmail"
    assert route.intent.arguments["operation"] == operation
    GmailInput.model_validate(route.intent.arguments)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("disabled", "installed but disabled"),
        ("disconnected", "no account is connected"),
        ("reauthorization_required", "needs additional account access"),
        ("unavailable", "not installed"),
    ],
)
def test_gmail_request_explains_unavailable_extension_state(status, message):
    route = DeterministicRouter(skill_status=lambda _skill_id: status).route("check my gmail")

    assert route.intent.name == "conversation.answer"
    assert message in route.intent.arguments["text"]


def test_gmail_input_rejects_incomplete_or_ambiguous_mutations():
    with pytest.raises(ValueError, match="requires message_id"):
        GmailInput(operation="trash_message")
    with pytest.raises(ValueError, match="at least one recipient"):
        GmailInput(operation="send_message", subject="Hello", body="Hi")
    with pytest.raises(ValueError, match="requires add_label_ids"):
        GmailInput(operation="modify_message", message_id="message-1")
    with pytest.raises(ValueError, match="same label"):
        GmailInput(
            operation="modify_message",
            message_id="message-1",
            add_label_ids=("STARRED",),
            remove_label_ids=("STARRED",),
        )
    with pytest.raises(ValueError, match="line breaks"):
        GmailInput(
            operation="send_message",
            to=("person@example.com",),
            subject="Hello\r\nBcc: attacker@example.com",
            body="Hi",
        )


def test_gmail_permissions_and_risk_are_operation_specific():
    skill = GmailSkill(RecordingAdapter())
    read = call({"operation": "get_message", "message_id": "message-1"})
    archive = call({"operation": "archive_message", "message_id": "message-1"})
    send = call(
        {
            "operation": "send_message",
            "to": ["person@example.com"],
            "subject": "Status",
            "body": "All done",
        }
    )

    assert skill.required_permissions(read)[0].capability == "integration.read"
    assert skill.required_permissions(read)[0].targets == ("gmail:message:message-1",)
    assert skill.required_permissions(archive)[0].capability == "integration.write"
    assert skill.required_permissions(send)[0].capability == "messaging"
    assert skill.required_permissions(send)[0].targets == (
        "gmail:recipients:person@example.com",
    )
    assert skill.risk_for(read) == RiskLevel.LOW
    assert skill.risk_for(archive) == RiskLevel.MEDIUM
    assert skill.risk_for(send) == RiskLevel.HIGH


@pytest.mark.asyncio
async def test_list_messages_hydrates_metadata_and_returns_sensitive_bounded_items():
    adapter = RecordingAdapter(
        [
            {"messages": [{"id": "message-1", "threadId": "thread-1"}], "nextPageToken": "next"},
            {
                "id": "message-1",
                "threadId": "thread-1",
                "snippet": "Private status update",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Person <person@example.com>"},
                        {"name": "Subject", "value": "Quarterly status"},
                        {"name": "Date", "value": "Sun, 2 Aug 2026 10:00:00 -0500"},
                    ]
                },
            },
        ]
    )
    skill = GmailSkill(adapter)

    result = await skill.execute(
        call({"operation": "list_messages", "query": "in:inbox is:unread", "max_results": 10}),
        asyncio.Event(),
    )

    assert adapter.requests[0]["path"] == "/users/me/messages"
    assert adapter.requests[0]["query"]["q"] == "in:inbox is:unread"
    assert adapter.requests[1]["query"]["format"] == "metadata"
    assert result.data["next_page_token"] == "next"
    assert result.items[0].title == "Quarterly status"
    assert result.items[0].value["message_id"] == "message-1"
    assert result.items[0].sensitive is True
    assert result.sensitive is True


@pytest.mark.asyncio
async def test_get_message_decodes_text_but_does_not_return_raw_mime_payload():
    encoded = base64.urlsafe_b64encode(b"Confidential body").decode().rstrip("=")
    adapter = RecordingAdapter(
        [
            {
                "id": "message-1",
                "threadId": "thread-1",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Subject", "value": "Status"}],
                    "body": {"data": encoded, "size": 17},
                },
            }
        ]
    )

    result = await GmailSkill(adapter).execute(
        call({"operation": "get_message", "message_id": "message-1"}),
        asyncio.Event(),
    )

    resource = result.data["resources"][0]
    assert resource["body"] == "Confidential body"
    assert "payload" not in resource


@pytest.mark.asyncio
async def test_send_message_builds_base64url_mime_and_uses_messaging_result():
    adapter = RecordingAdapter([{"id": "sent-1", "threadId": "thread-1"}])
    skill = GmailSkill(adapter, account=lambda: "sender@example.com")

    result = await skill.execute(
        call(
            {
                "operation": "send_message",
                "to": ["person@example.com"],
                "cc": ["copy@example.com"],
                "subject": "Status",
                "body": "All done",
            }
        ),
        asyncio.Event(),
    )

    request = adapter.requests[0]
    raw = request["body"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    message = BytesParser(policy=policy.default).parsebytes(decoded)
    assert request["path"] == "/users/me/messages/send"
    assert message["To"] == "person@example.com"
    assert message["From"] == "sender@example.com"
    assert message["Cc"] == "copy@example.com"
    assert message["Subject"] == "Status"
    assert message.get_content().strip() == "All done"
    assert result.summary == "Gmail sent the email."


@pytest.mark.asyncio
async def test_reply_uses_original_sender_and_threading_headers():
    adapter = RecordingAdapter(
        [
            {
                "id": "message-1",
                "threadId": "thread-1",
                "payload": {
                    "headers": [
                        {"name": "Reply-To", "value": "reply@example.com"},
                        {"name": "Subject", "value": "Status"},
                        {"name": "Message-ID", "value": "<original@example.com>"},
                    ]
                },
            },
            {"id": "sent-2", "threadId": "thread-1"},
        ]
    )
    skill = GmailSkill(adapter, account=lambda: "sender@example.com")

    await skill.execute(
        call({"operation": "reply_message", "message_id": "message-1", "body": "Thanks"}),
        asyncio.Event(),
    )

    request = adapter.requests[1]
    raw = request["body"]["raw"]
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )
    assert request["body"]["threadId"] == "thread-1"
    assert message["To"] == "reply@example.com"
    assert message["Subject"] == "Re: Status"
    assert message["In-Reply-To"] == "<original@example.com>"
    assert message["References"] == "<original@example.com>"


@pytest.mark.asyncio
async def test_archive_message_removes_only_the_inbox_label():
    adapter = RecordingAdapter([{"id": "message-1", "labelIds": ["IMPORTANT"]}])

    await GmailSkill(adapter).execute(
        call({"operation": "archive_message", "message_id": "message-1"}),
        asyncio.Event(),
    )

    assert adapter.requests[0] == {
        "method": "POST",
        "path": "/users/me/messages/message-1/modify",
        "query": None,
        "body": {"addLabelIds": [], "removeLabelIds": ["INBOX"]},
    }


@pytest.mark.asyncio
async def test_token_manager_refreshes_and_persists_separate_gmail_token():
    vault = MemoryVault({"gmail_client_id": "client", "gmail_refresh_token": "refresh"})
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return Response({"access_token": "new-access", "expires_in": 3600})

    manager = GmailTokenManager(vault, opener=opener, clock=lambda: 1000)

    assert await manager.access_token() == "new-access"
    assert vault.get("gmail_access_token") == "new-access"
    assert float(vault.get("gmail_access_token_expires_at")) == 4600
    assert "google_calendar_access_token" not in vault.values
    assert b"refresh_token=refresh" in requests[0][0].data


@pytest.mark.asyncio
async def test_adapter_retries_one_unauthorized_request_with_fresh_token():
    class Tokens:
        def __init__(self):
            self.force_refresh = []

        async def access_token(self, *, force_refresh=False):
            self.force_refresh.append(force_refresh)
            return "fresh" if force_refresh else "expired"

    requests = []

    def opener(request, timeout):
        requests.append(request)
        if request.headers["Authorization"] == "Bearer expired":
            payload = json.dumps({"error": {"message": "Invalid Credentials"}}).encode()
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, io.BytesIO(payload)
            )
        return Response({"emailAddress": "person@example.com"})

    tokens = Tokens()
    adapter = GmailAdapter(tokens, opener=opener)

    assert await adapter.request("GET", "/users/me/profile") == {
        "emailAddress": "person@example.com"
    }
    assert tokens.force_refresh == [False, True]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_connection_service_completes_pkce_with_gmail_scope_and_separate_tokens():
    vault = MemoryVault()
    requests = []

    def opener(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/token"):
            return Response(
                {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
            )
        if request.full_url.endswith("/users/me/profile"):
            return Response({"emailAddress": "person@example.com"})
        if request.full_url.endswith("/revoke"):
            return Response({})
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

    service = GmailConnectionService(vault, opener=opener, browser_open=browser_open)

    status = await service.connect(client_secret="desktop-secret")

    assert status.connected is True
    assert status.account == "person@example.com"
    assert authorization["scope"] == [GMAIL_SCOPE]
    assert authorization["code_challenge_method"] == ["S256"]
    assert authorization["client_id"] == [DEFAULT_GMAIL_CLIENT_ID]
    assert vault.get("gmail_refresh_token") == "refresh"
    assert vault.get("gmail_access_token") == "access"
    assert "google_calendar_refresh_token" not in vault.values

    disconnected = await service.disconnect()
    assert disconnected.connected is False
    assert vault.values == {
        "gmail_client_id": DEFAULT_GMAIL_CLIENT_ID,
        "gmail_client_secret": "desktop-secret",
        "gmail_oauth_client_id": DEFAULT_GMAIL_CLIENT_ID,
        "gmail_oauth_client_secret": "desktop-secret",
    }


@pytest.mark.asyncio
async def test_gmail_is_installed_from_hub_and_connected_install_survives_restart(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        offline_lock=False,
        embedding_provider="lexical",
        llm_provider="none",
    )
    container = build_container(settings)

    with pytest.raises(KeyError):
        container.registry.inspect("integration.gmail")
    entry = next(
        item
        for item in container.extension_hub.catalog.extensions
        if item.extension_id == "integration.gmail"
    )
    assert entry.source == "first_party"
    assert entry.featured is True

    manifest = await container.extension_hub.install(entry.extension_id)
    connection = container.extension_hub.connection_service(entry.extension_id)
    connection.vault.set("gmail_client_id", "desktop-client")
    connection.vault.set("gmail_refresh_token", "refresh-token")

    assert manifest.skill_id == "integration.gmail"
    assert container.registry.is_enabled("integration.gmail") is False

    restarted = build_container(settings)

    assert restarted.registry.is_enabled("integration.gmail") is True
