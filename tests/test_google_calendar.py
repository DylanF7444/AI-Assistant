from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

import pytest

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.contracts import PermissionContext, RiskLevel, SkillCall
from assistant.integrations.google_calendar import (
    DEFAULT_GOOGLE_CALENDAR_CLIENT_ID,
    GoogleCalendarAdapter,
    GoogleCalendarConnectionService,
    GoogleCalendarError,
    GoogleCalendarTokenManager,
)
from assistant.language.planner import SkillRetriever
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.google_calendar import GoogleCalendarInput, GoogleCalendarSkill


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


def call(arguments):
    return SkillCall(
        skill_id="integration.google_calendar",
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("check my calendar", "list_events"),
        ("Could you please check my calendar?", "list_events"),
        ("what is on my calendar today", "list_events"),
        ("Whats on my Google Calendar?", "list_events"),
        ("What's coming up on my calendar?", "list_events"),
        ("show my appointments tomorrow", "list_events"),
        ("what meetings do I have tomorrow", "list_events"),
        ("show my schedule today", "list_events"),
        ("add dentist tomorrow at 3 to my calendar", "quick_add"),
        ("Can you add dentist tomorrow at 3 to my calendar?", "quick_add"),
        ("add dentist to my calendar tomorrow at 3", "quick_add"),
        ("put team standup Friday at 9 on my Google Calendar", "quick_add"),
        ("schedule a meeting with Sam Friday at 2 on my calendar", "quick_add"),
        ("create an appointment for an annual physical next Tuesday at 10", "quick_add"),
        ("find quarterly planning on my calendar", "list_events"),
        ("list my Google calendars", "list_calendars"),
        ("create a new calendar called Family", "create_calendar"),
    ],
)
def test_common_calendar_requests_route_without_the_llm(text, operation):
    router = DeterministicRouter(
        clock=lambda: datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
    )

    route = router.route(text)

    assert route.needs_llm is False
    assert route.clarification is None
    assert route.intent.name == "integration.google_calendar"
    assert route.intent.arguments["operation"] == operation
    GoogleCalendarInput.model_validate(route.intent.arguments)


def test_calendar_read_routes_use_bounded_relative_time_windows():
    router = DeterministicRouter(
        clock=lambda: datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
    )

    today = router.route("what is on my calendar today").intent.arguments
    tomorrow = router.route("show my appointments tomorrow").intent.arguments

    assert today["time_min"] == "2026-07-30T20:00:00+00:00"
    assert today["time_max"] == "2026-07-31T00:00:00+00:00"
    assert tomorrow["time_min"] == "2026-07-31T00:00:00+00:00"
    assert tomorrow["time_max"] == "2026-08-01T00:00:00+00:00"
    assert today["max_results"] == 10


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("disabled", "installed but disabled"),
        ("disconnected", "no account is connected"),
        ("unavailable", "not installed"),
    ],
)
def test_calendar_request_explains_unavailable_extension_state(status, message):
    route = DeterministicRouter(skill_status=lambda _skill_id: status).route(
        "check my calendar"
    )

    assert route.intent.name == "conversation.answer"
    assert message in route.intent.arguments["text"]
    assert route.needs_llm is False


def test_calendar_synonyms_retrieve_the_calendar_skill_for_complex_requests():
    manifest = GoogleCalendarSkill(RecordingAdapter()).manifest
    retriever = SkillRetriever((manifest,))

    for text in (
        "book an appointment next Tuesday",
        "move the selected meeting",
        "show my agenda",
    ):
        assert retriever.relevant(text, limit=1)[0].skill_id == (
            "integration.google_calendar"
        )


class RecordingAdapter:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    async def request(self, method, path, *, query=None, body=None):
        self.requests.append({"method": method, "path": path, "query": query, "body": body})
        return self.responses.pop(0) if self.responses else {}

    async def health(self):
        return {"healthy": True}


def test_google_calendar_input_rejects_ambiguous_or_incomplete_changes():
    with pytest.raises(ValueError, match="requires event_id"):
        GoogleCalendarInput(operation="delete_event")
    with pytest.raises(ValueError, match="summary, start, and end"):
        GoogleCalendarInput(operation="create_event", summary="Incomplete")
    with pytest.raises(ValueError, match="sync_token"):
        GoogleCalendarInput(
            operation="list_events",
            sync_token="sync",
            query="planning",
        )
    with pytest.raises(ValueError, match="scope_value"):
        GoogleCalendarInput(
            operation="create_access_rule",
            scope_type="user",
            role="reader",
        )


def test_google_calendar_permissions_and_risk_are_operation_specific():
    skill = GoogleCalendarSkill(RecordingAdapter())
    read = call({"operation": "list_events", "calendar_id": "work@example.com"})
    create = call(
        {
            "operation": "create_event",
            "calendar_id": "work@example.com",
            "summary": "Planning",
            "start": "2026-08-01T10:00:00-05:00",
            "end": "2026-08-01T11:00:00-05:00",
        }
    )
    clear = call({"operation": "clear_calendar", "calendar_id": "primary"})

    read_permissions = skill.required_permissions(read)
    write_permissions = skill.required_permissions(create)

    assert read_permissions[0].capability == "integration.read"
    assert read_permissions[0].targets == ("google-calendar:work@example.com:*",)
    assert write_permissions[0].capability == "integration.write"
    assert skill.risk_for(read) == RiskLevel.LOW
    assert skill.risk_for(create) == RiskLevel.MEDIUM
    assert skill.risk_for(clear) == RiskLevel.CRITICAL


@pytest.mark.asyncio
async def test_list_events_uses_exact_calendar_and_returns_sensitive_typed_items():
    adapter = RecordingAdapter(
        [
            {
                "items": [
                    {
                        "id": "event-1",
                        "summary": "Private planning",
                        "start": {"dateTime": "2026-08-01T10:00:00-05:00"},
                    }
                ],
                "nextPageToken": "next",
            }
        ]
    )
    skill = GoogleCalendarSkill(adapter)

    result = await skill.execute(
        call(
            {
                "operation": "list_events",
                "calendar_id": "work@example.com",
                "time_min": "2026-08-01T00:00:00-05:00",
                "time_max": "2026-08-02T00:00:00-05:00",
                "order_by": "startTime",
            }
        ),
        asyncio.Event(),
    )

    request = adapter.requests[0]
    assert request["path"] == "/calendars/work%40example.com/events"
    assert request["query"]["orderBy"] == "startTime"
    assert result.data["next_page_token"] == "next"
    assert result.items[0].value["event_id"] == "event-1"
    assert result.items[0].sensitive is True
    assert result.sensitive is True


@pytest.mark.asyncio
async def test_empty_list_events_returns_a_human_friendly_calendar_message():
    skill = GoogleCalendarSkill(RecordingAdapter([{"items": []}]))

    result = await skill.execute(
        call({"operation": "list_events"}),
        asyncio.Event(),
    )

    assert result.summary == "I couldn't find any calendar events."
    assert result.display_text == "You don't have any upcoming events on your calendar."


@pytest.mark.asyncio
async def test_create_event_maps_attendees_reminders_recurrence_and_unique_meet_request():
    adapter = RecordingAdapter([{"id": "created", "summary": "Planning"}])
    skill = GoogleCalendarSkill(adapter)
    skill_call = call(
        {
            "operation": "create_event",
            "summary": "Planning",
            "start": "2026-08-01T10:00:00-05:00",
            "end": "2026-08-01T11:00:00-05:00",
            "attendees": ["person@example.com"],
            "reminder_minutes": [10],
            "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
            "add_google_meet": True,
        }
    )

    await skill.execute(skill_call, asyncio.Event())

    request = adapter.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/calendars/primary/events"
    assert request["query"]["conferenceDataVersion"] == 1
    assert request["body"]["attendees"] == [{"email": "person@example.com"}]
    assert request["body"]["reminders"]["overrides"][0]["minutes"] == 10
    assert request["body"]["conferenceData"]["createRequest"]["requestId"] == (
        skill_call.idempotency_key
    )


@pytest.mark.asyncio
async def test_rsvp_preserves_attendees_and_changes_only_self():
    adapter = RecordingAdapter(
        [
            {
                "id": "event",
                "attendees": [
                    {"email": "self@example.com", "self": True, "responseStatus": "needsAction"},
                    {"email": "other@example.com", "responseStatus": "accepted"},
                ],
            },
            {"id": "event", "summary": "Invitation"},
        ]
    )
    skill = GoogleCalendarSkill(adapter)

    await skill.execute(
        call(
            {
                "operation": "respond_to_event",
                "event_id": "event",
                "response": "accepted",
            }
        ),
        asyncio.Event(),
    )

    patch = adapter.requests[1]
    assert patch["method"] == "PATCH"
    assert patch["body"]["attendees"][0]["responseStatus"] == "accepted"
    assert patch["body"]["attendees"][1]["responseStatus"] == "accepted"


@pytest.mark.asyncio
async def test_admin_calendar_transfer_is_exact_and_critical():
    adapter = RecordingAdapter()
    skill = GoogleCalendarSkill(adapter)
    skill_call = call(
        {
            "operation": "transfer_calendar_ownership",
            "calendar_id": "team@example.com",
            "new_data_owner": "new-owner@example.com",
        }
    )

    assert skill.risk_for(skill_call) == RiskLevel.CRITICAL
    await skill.execute(skill_call, asyncio.Event())

    request = adapter.requests[0]
    assert request["path"] == "/calendars/team%40example.com/transferOwnership"
    assert request["query"] == {
        "newDataOwner": "new-owner@example.com",
        "useAdminAccess": True,
    }


@pytest.mark.asyncio
async def test_token_manager_refreshes_and_persists_access_token():
    vault = MemoryVault(
        {
            "google_calendar_client_id": "client",
            "google_calendar_refresh_token": "refresh",
        }
    )
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return Response({"access_token": "new-access", "expires_in": 3600})

    manager = GoogleCalendarTokenManager(
        vault,
        opener=opener,
        clock=lambda: 1000,
    )

    assert await manager.access_token() == "new-access"
    assert vault.get("google_calendar_access_token") == "new-access"
    assert float(vault.get("google_calendar_access_token_expires_at")) == 4600
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
                request.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(payload),
            )
        return Response({"items": []})

    tokens = Tokens()
    adapter = GoogleCalendarAdapter(tokens, opener=opener)

    assert await adapter.request("GET", "/users/me/calendarList") == {"items": []}
    assert tokens.force_refresh == [False, True]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_adapter_redacts_structured_google_error_to_bounded_message():
    def opener(request, timeout):
        payload = json.dumps({"error": {"message": "Calendar not found"}}).encode()
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "Not Found",
            {},
            io.BytesIO(payload),
        )

    class Tokens:
        async def access_token(self, *, force_refresh=False):
            return "token"

    adapter = GoogleCalendarAdapter(Tokens(), opener=opener)

    with pytest.raises(GoogleCalendarError, match="Calendar not found") as captured:
        await adapter.request("GET", "/calendars/missing")
    assert captured.value.status == 404
    assert captured.value.recoverable is False


@pytest.mark.asyncio
async def test_connection_service_completes_pkce_loopback_and_encrypts_account_tokens():
    vault = MemoryVault()
    requests = []

    def opener(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/token"):
            return Response(
                {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                }
            )
        if request.full_url.endswith("/calendars/primary"):
            return Response({"id": "person@example.com"})
        if request.full_url.endswith("/revoke"):
            return Response({})
        raise AssertionError(request.full_url)

    authorization = {}
    callback_page = {}

    def browser_open(url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        authorization.update(query)
        callback = (
            query["redirect_uri"][0]
            + "?"
            + urllib.parse.urlencode({"state": query["state"][0], "code": "authorization-code"})
        )
        with urllib.request.urlopen(callback, timeout=5) as response:
            callback_page["body"] = response.read().decode("utf-8")
        return True

    service = GoogleCalendarConnectionService(
        vault,
        opener=opener,
        browser_open=browser_open,
    )

    status = await service.connect(client_secret="desktop-secret")

    assert status.connected is True
    assert status.account == "person@example.com"
    assert authorization["code_challenge_method"] == ["S256"]
    assert authorization["access_type"] == ["offline"]
    assert authorization["client_id"] == [DEFAULT_GOOGLE_CALENDAR_CLIENT_ID]
    assert vault.get("google_calendar_client_id") == DEFAULT_GOOGLE_CALENDAR_CLIENT_ID
    assert vault.get("google_calendar_client_secret") == "desktop-secret"
    assert vault.get("google_calendar_oauth_client_id") == DEFAULT_GOOGLE_CALENDAR_CLIENT_ID
    assert vault.get("google_calendar_oauth_client_secret") == "desktop-secret"
    assert vault.get("google_calendar_refresh_token") == "refresh"
    assert vault.get("google_calendar_access_token") == "access"
    assert "Google Calendar connected" in callback_page["body"]
    token_request = next(request for request in requests if request.full_url.endswith("/token"))
    assert b"code_verifier=" in token_request.data
    assert b"client_secret=desktop-secret" in token_request.data

    disconnected = await service.disconnect()
    assert disconnected.connected is False
    assert vault.values == {
        "google_calendar_client_id": DEFAULT_GOOGLE_CALENDAR_CLIENT_ID,
        "google_calendar_client_secret": "desktop-secret",
        "google_calendar_oauth_client_id": DEFAULT_GOOGLE_CALENDAR_CLIENT_ID,
        "google_calendar_oauth_client_secret": "desktop-secret",
    }

    requests.clear()
    reconnected = await service.connect()
    assert reconnected.connected is True
    token_request = next(request for request in requests if request.full_url.endswith("/token"))
    assert b"client_secret=desktop-secret" in token_request.data


@pytest.mark.asyncio
async def test_connection_service_remembers_client_secret_when_first_exchange_fails():
    vault = MemoryVault()
    callback_page = {}

    def opener(request, timeout):
        payload = json.dumps(
            {"error": {"message": "client_secret is missing"}}
        ).encode()
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(payload),
        )

    def browser_open(url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        callback = (
            query["redirect_uri"][0]
            + "?"
            + urllib.parse.urlencode(
                {"state": query["state"][0], "code": "authorization-code"}
            )
        )
        try:
            urllib.request.urlopen(callback, timeout=5)
        except urllib.error.HTTPError as error:
            callback_page["body"] = error.read().decode("utf-8")
        return True

    service = GoogleCalendarConnectionService(
        vault,
        opener=opener,
        browser_open=browser_open,
    )

    with pytest.raises(GoogleCalendarError, match="client_secret is missing"):
        await service.connect(client_secret="desktop-secret")

    assert vault.values == {
        "google_calendar_oauth_client_id": DEFAULT_GOOGLE_CALENDAR_CLIENT_ID,
        "google_calendar_oauth_client_secret": "desktop-secret",
    }
    assert "Enter it once" in callback_page["body"]


@pytest.mark.asyncio
async def test_connection_callback_reports_token_exchange_failure_without_false_success():
    vault = MemoryVault()
    callback_page = {}

    def opener(request, timeout):
        payload = json.dumps(
            {
                "error": "invalid_client",
                "error_description": "The OAuth client could not be authenticated.",
            }
        ).encode()
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(payload),
        )

    def browser_open(url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        callback = (
            query["redirect_uri"][0]
            + "?"
            + urllib.parse.urlencode(
                {"state": query["state"][0], "code": "authorization-code"}
            )
        )
        try:
            urllib.request.urlopen(callback, timeout=5)
        except urllib.error.HTTPError as error:
            callback_page["status"] = error.code
            callback_page["body"] = error.read().decode("utf-8")
        return True

    service = GoogleCalendarConnectionService(
        vault,
        opener=opener,
        browser_open=browser_open,
    )

    with pytest.raises(
        GoogleCalendarError,
        match="The OAuth client could not be authenticated",
    ):
        await service.connect()

    assert callback_page["status"] == 400
    assert "Google Calendar was not connected" in callback_page["body"]
    assert "Google Calendar connected</h1>" not in callback_page["body"]
    assert "could not be authenticated" in callback_page["body"]
    assert vault.values == {}


@pytest.mark.asyncio
async def test_google_calendar_is_installed_from_hub_not_registered_by_default(tmp_path):
    container = build_container(
        Settings(
            profile="testing",
            data_dir=tmp_path,
            offline_lock=True,
            embedding_provider="lexical",
        )
    )

    with pytest.raises(KeyError):
        container.registry.inspect("integration.google_calendar")
    entry = next(
        item
        for item in container.extension_hub.catalog.extensions
        if item.extension_id == "integration.google_calendar"
    )
    assert entry.source == "first_party"
    assert container.extension_hub.installation_status(entry.extension_id) == "available"

    manifest = await container.extension_hub.install(entry.extension_id)

    assert manifest.skill_id == entry.extension_id
    assert container.registry.inspect(entry.extension_id).manifest == manifest
    assert container.registry.is_enabled(entry.extension_id) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("check my calendar", "list_events"),
        ("Whats on my Google Calendar?", "list_events"),
        ("add dentist tomorrow at 3 to my calendar", "quick_add"),
    ],
)
async def test_calendar_requests_prepare_the_extension_skill_without_a_planner(
    tmp_path,
    text,
    operation,
):
    container = build_container(
        Settings(
            profile="testing",
            data_dir=tmp_path,
            offline_lock=False,
            embedding_provider="lexical",
            llm_provider="none",
        )
    )
    await container.extension_hub.install("integration.google_calendar")
    connection = container.extension_hub.connection_service(
        "integration.google_calendar"
    )
    connection.vault.set("google_calendar_client_id", "desktop-client")
    connection.vault.set("google_calendar_refresh_token", "refresh-token")
    container.registry.enable("integration.google_calendar")

    _response, outcome = await container.orchestrator.handle_text(text)

    assert outcome.call is not None
    assert outcome.call.skill_id == "integration.google_calendar"
    assert outcome.call.arguments["operation"] == operation
    assert outcome.permission_requests


@pytest.mark.asyncio
async def test_calendar_creation_clarification_completes_without_a_planner(tmp_path):
    container = build_container(
        Settings(
            profile="testing",
            data_dir=tmp_path,
            offline_lock=False,
            embedding_provider="lexical",
            llm_provider="none",
        )
    )
    await container.extension_hub.install("integration.google_calendar")
    connection = container.extension_hub.connection_service(
        "integration.google_calendar"
    )
    connection.vault.set("google_calendar_client_id", "desktop-client")
    connection.vault.set("google_calendar_refresh_token", "refresh-token")
    container.registry.enable("integration.google_calendar")

    first_response, first_outcome = await container.orchestrator.handle_text(
        "add to my calendar"
    )
    second_response, second_outcome = await container.orchestrator.handle_text(
        "dentist tomorrow at 3"
    )

    assert "What event should" in first_response.display_text
    assert first_outcome.call is None
    assert second_outcome.call is not None
    assert second_outcome.call.skill_id == "integration.google_calendar"
    assert second_outcome.call.arguments == {
        "operation": "quick_add",
        "calendar_id": "primary",
        "quick_add_text": "dentist tomorrow at 3",
    }
    assert second_response.response_type.value == "permission"


@pytest.mark.asyncio
async def test_connected_calendar_extension_is_enabled_after_restart(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        offline_lock=False,
        embedding_provider="lexical",
        llm_provider="none",
    )
    container = build_container(settings)
    await container.extension_hub.install("integration.google_calendar")
    connection = container.extension_hub.connection_service(
        "integration.google_calendar"
    )
    connection.vault.set("google_calendar_client_id", "desktop-client")
    connection.vault.set("google_calendar_refresh_token", "refresh-token")

    restarted = build_container(settings)

    assert restarted.registry.is_enabled("integration.google_calendar") is True
