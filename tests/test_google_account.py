from __future__ import annotations

import io
import json
import urllib.parse
import urllib.request

import pytest

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.integrations.gmail import GMAIL_SCOPE
from assistant.integrations.google import (
    GOOGLE_ACCOUNT_SCOPES,
    GOOGLE_SECRETS,
    USERINFO_URL,
    GoogleConnectionService,
    GoogleTokenManager,
)
from assistant.integrations.google_calendar import CALENDAR_SCOPE
from assistant.integrations.google_drive import DRIVE_READ_SCOPE


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


def _complete_callback(url: str) -> None:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    callback = query["redirect_uri"][0] + "?" + urllib.parse.urlencode(
        {"state": query["state"][0], "code": "authorization-code"}
    )
    with urllib.request.urlopen(callback, timeout=5):
        pass


@pytest.mark.asyncio
async def test_google_extensions_share_one_connection_and_keep_scope_boundaries(tmp_path):
    container = build_container(
        Settings(
            profile="testing",
            data_dir=tmp_path,
            offline_lock=False,
            embedding_provider="lexical",
            llm_provider="none",
        )
    )

    services = tuple(
        container.extension_hub.connection_service(extension_id)
        for extension_id in (
            "integration.google_calendar",
            "integration.gmail",
            "integration.google_drive",
        )
    )
    assert services[0] is services[1] is services[2]
    assert services[0].provider_id == "google"

    services[0].vault.set(GOOGLE_SECRETS.client_id, "desktop-client")
    services[0].vault.set(GOOGLE_SECRETS.refresh_token, "refresh-token")
    services[0].vault.set(
        GOOGLE_SECRETS.granted_scopes,
        json.dumps((*GOOGLE_ACCOUNT_SCOPES, CALENDAR_SCOPE)),
    )
    await container.extension_hub.install("integration.google_calendar")
    await container.extension_hub.install("integration.gmail")
    await container.extension_hub.install("integration.google_drive")

    assert container.extension_hub.connection_status(
        "integration.google_calendar"
    ).usable is True
    assert container.extension_hub.connection_status("integration.gmail").missing_scopes == (
        GMAIL_SCOPE,
    )
    assert container.extension_hub.connection_status(
        "integration.google_drive"
    ).missing_scopes == (DRIVE_READ_SCOPE,)


@pytest.mark.asyncio
async def test_google_connection_reuses_account_and_adds_product_scopes_incrementally():
    vault = MemoryVault()
    authorization_queries = []
    token_count = 0

    def opener(request, timeout):
        nonlocal token_count
        if request.full_url.endswith("/token"):
            token_count += 1
            payload = {
                "access_token": f"access-{token_count}",
                "expires_in": 3600,
            }
            if token_count == 1:
                payload["refresh_token"] = "refresh-token"
            return Response(payload)
        if request.full_url == USERINFO_URL:
            return Response({"email": "person@example.com"})
        if request.full_url.endswith("/revoke"):
            return Response({})
        raise AssertionError(request.full_url)

    def browser_open(url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        authorization_queries.append(query)
        _complete_callback(url)
        return True

    service = GoogleConnectionService(vault, opener=opener, browser_open=browser_open)
    first = await service.connect(
        client_id="desktop-client",
        required_scopes=(CALENDAR_SCOPE,),
    )
    second = await service.connect(required_scopes=(GMAIL_SCOPE,))
    already_connected = await service.connect(required_scopes=(CALENDAR_SCOPE,))

    assert first.usable is True
    assert second.usable is True
    assert already_connected.usable is True
    assert first.account == second.account == "person@example.com"
    assert len(authorization_queries) == 2
    assert set(authorization_queries[0]["scope"][0].split()) == {
        *GOOGLE_ACCOUNT_SCOPES,
        CALENDAR_SCOPE,
    }
    assert set(authorization_queries[1]["scope"][0].split()) == {
        *GOOGLE_ACCOUNT_SCOPES,
        CALENDAR_SCOPE,
        GMAIL_SCOPE,
    }
    assert authorization_queries[1]["login_hint"] == ["person@example.com"]
    assert vault.get(GOOGLE_SECRETS.refresh_token) == "refresh-token"
    assert vault.get(GOOGLE_SECRETS.account) == "person@example.com"
    assert token_count == 2

    disconnected = await service.disconnect()
    assert disconnected.connected is False
    assert vault.get(GOOGLE_SECRETS.refresh_token) is None


def test_legacy_google_authorization_migrates_only_an_unambiguous_account():
    vault = MemoryVault(
        {
            "google_calendar_client_id": "calendar-client",
            "google_calendar_refresh_token": "calendar-refresh",
            "google_calendar_account": "person@example.com",
            "google_calendar_granted_scopes": json.dumps([CALENDAR_SCOPE]),
            "gmail_client_id": "gmail-client",
            "gmail_refresh_token": "gmail-refresh",
            "gmail_account": "person@example.com",
            "gmail_granted_scopes": json.dumps([GMAIL_SCOPE]),
        }
    )

    assert GoogleTokenManager(vault).connected() is True
    assert vault.get(GOOGLE_SECRETS.client_id) == "calendar-client"
    assert vault.get(GOOGLE_SECRETS.refresh_token) == "calendar-refresh"
    assert json.loads(vault.get(GOOGLE_SECRETS.granted_scopes)) == [CALENDAR_SCOPE]

    different_accounts = MemoryVault(
        {
            "google_calendar_client_id": "calendar-client",
            "google_calendar_refresh_token": "calendar-refresh",
            "google_calendar_account": "calendar@example.com",
            "gmail_client_id": "gmail-client",
            "gmail_refresh_token": "gmail-refresh",
            "gmail_account": "gmail@example.com",
        }
    )
    assert GoogleTokenManager(different_accounts).connected() is False


@pytest.mark.asyncio
async def test_shared_google_token_manager_refreshes_common_secret_names():
    vault = MemoryVault(
        {
            GOOGLE_SECRETS.client_id: "client",
            GOOGLE_SECRETS.refresh_token: "refresh",
        }
    )
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return Response({"access_token": "new-access", "expires_in": 3600})

    manager = GoogleTokenManager(vault, opener=opener, clock=lambda: 1000)

    assert await manager.access_token() == "new-access"
    assert vault.get(GOOGLE_SECRETS.access_token) == "new-access"
    assert b"refresh_token=refresh" in requests[0][0].data
