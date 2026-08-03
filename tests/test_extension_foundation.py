from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import BaseModel

from assistant.contracts import ResultStatus, SkillCall, SkillResult
from assistant.extensions.platform import (
    ExtensionHubService,
    ExtensionPlatformClient,
    ManagedExtensionRegistry,
    ManagedExtensionSpec,
)
from assistant.integrations.connections import (
    ConnectionBinding,
    ConnectionProfile,
    ConnectionState,
    ConnectionStatus,
)
from assistant.integrations.http import ExternalJsonClient
from assistant.integrations.oauth import (
    OAuthLoopbackMessages,
    OAuthSecretNames,
    OAuthTokenManager,
    PKCEAuthorization,
    run_loopback_authorization,
)
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.skills.registry import SkillRegistry
from assistant.storage.connections import ConnectionProfileStore
from assistant.storage.database import Database, SettingsStore
from assistant.storage.secrets import SecretVault


class _Input(BaseModel):
    value: str = "test"


class _Output(BaseModel):
    value: str


def _skill_factory(skill_id: str):
    class _ManagedSkill(Skill):
        input_model = _Input
        output_model = _Output

        def __init__(self) -> None:
            self.manifest = manifest(
                skill_id=skill_id,
                display_name=skill_id.rsplit(".", 1)[-1].title(),
                description=f"Exercise {skill_id} connection behavior.",
                input_model=self.input_model,
                output_model=self.output_model,
            ).model_copy(update={"entry_point": f"{__name__}:_ManagedSkill"})

        async def execute(
            self,
            call: SkillCall,
            cancel_event: asyncio.Event,
        ) -> SkillResult:
            arguments = self.validate(call.arguments)
            return SkillResult(
                status=ResultStatus.COMPLETED,
                summary="Done.",
                data={"value": arguments.value},
            )

    return _ManagedSkill


class _ScopedConnection:
    provider_id = "example"

    def __init__(self, granted_scopes: tuple[str, ...]) -> None:
        self.granted_scopes = granted_scopes
        self.status_calls = []

    def status(self, *, profile_id=None, required_scopes=()):
        self.status_calls.append((profile_id, required_scopes))
        return ConnectionStatus(
            connected=True,
            account="person@example.com",
            granted_scopes=self.granted_scopes,
        )

    async def test(self):
        return self.status()

    async def disconnect(self):
        return ConnectionStatus(connected=False)


class _ConnectableScopedConnection(_ScopedConnection):
    def __init__(self, granted_scopes: tuple[str, ...] = ()) -> None:
        super().__init__(granted_scopes)
        self.connect_calls = []

    async def connect(self, client_id=None, *, client_secret=None, profile_id=None, required_scopes=()):
        self.connect_calls.append(
            (client_id, client_secret, profile_id, tuple(required_scopes))
        )
        return ConnectionStatus(
            connected=True,
            account="person@example.com",
            granted_scopes=required_scopes,
        )


@pytest.mark.asyncio
async def test_shared_connection_enables_only_extensions_with_granted_scopes(tmp_path):
    connection = _ScopedConnection(("calendar.read",))
    calendar_factory = _skill_factory("integration.example_calendar")
    mail_factory = _skill_factory("integration.example_mail")
    calendar = ManagedExtensionSpec.from_factory(
        calendar_factory,
        connection=ConnectionBinding(
            service=connection,
            provider_id="example",
            required_scopes=("calendar.read",),
            dialog_entry_point="example.ui:ConnectionDialog",
        ),
    )
    mail = ManagedExtensionSpec.from_factory(
        mail_factory,
        connection=ConnectionBinding(
            service=connection,
            provider_id="example",
            required_scopes=("mail.read",),
            dialog_entry_point="example.ui:ConnectionDialog",
        ),
    )
    managed = ManagedExtensionRegistry((calendar, mail))
    registry = SkillRegistry(
        tmp_path / "installed",
        managed_skill_factories=managed.skill_factories,
    )
    hub = ExtensionHubService(
        registry,
        ExtensionPlatformClient(None),
        managed_registry=managed,
    )
    await hub.install(calendar.extension_id)
    await hub.install(mail.extension_id)

    assert hub.sync_connected_extensions() == (calendar.extension_id,)
    assert registry.is_enabled(calendar.extension_id)
    assert not registry.is_enabled(mail.extension_id)
    assert hub.skill_availability(mail.extension_id) == "reauthorization_required"
    assert hub.connection_status(mail.extension_id).missing_scopes == ("mail.read",)
    assert hub.connection_dialog_entry_point(calendar.extension_id) == (
        "example.ui:ConnectionDialog"
    )
    assert connection.status_calls[-1][1] == ("mail.read",)


def test_connection_status_preserves_authentication_while_scopes_are_missing():
    status = ConnectionStatus(
        connected=True,
        account="person@example.com",
        granted_scopes=("profile",),
        required_scopes=("mail.read", "profile"),
    )

    assert status.state == ConnectionState.MISSING_SCOPES
    assert status.connected is True
    assert status.authenticated is True
    assert status.usable is False
    assert status.missing_scopes == ("mail.read",)


def test_connection_profile_rejects_credentials_in_plaintext_configuration():
    with pytest.raises(ValueError, match="SecretVault"):
        ConnectionProfile(
            provider_id="example",
            display_name="Example",
            configuration={"oauth": {"refresh_token": "plaintext"}},
        )


def test_connection_binding_rejects_provider_service_identity_mismatch():
    with pytest.raises(ValueError, match="does not match"):
        ConnectionBinding(
            service=_ScopedConnection(()),
            provider_id="different",
        )


@pytest.mark.asyncio
async def test_connection_dialog_service_injects_profile_and_required_scopes():
    connection = _ConnectableScopedConnection()
    profile_id = UUID("11111111-1111-1111-1111-111111111111")
    binding = ConnectionBinding(
        service=connection,
        provider_id="example",
        profile_id=profile_id,
        required_scopes=("mail.read",),
    )

    status = await binding.dialog_service().connect(
        "client",
        client_secret="secret",
    )

    assert connection.connect_calls == [
        ("client", "secret", profile_id, ("mail.read",)),
    ]
    assert status.usable is True


def test_legacy_managed_connections_preserve_declared_provider_identity(tmp_path):
    factory = _skill_factory("integration.legacy_example")
    spec = ManagedExtensionSpec.from_factory(factory)
    registry = SkillRegistry(
        tmp_path / "installed",
        managed_skill_factories={spec.extension_id: factory},
    )
    service = _ScopedConnection(())

    hub = ExtensionHubService(
        registry,
        ExtensionPlatformClient(None),
        managed_extensions=((spec.extension, spec.manifest),),
        connection_services={spec.extension_id: service},
    )

    assert hub.connection_binding(spec.extension_id).provider_id == service.provider_id


class _Cipher:
    def encrypt(self, plaintext: str) -> bytes:
        return plaintext[::-1].encode("utf-8")

    def decrypt(self, ciphertext: bytes) -> str:
        return ciphertext.decode("utf-8")[::-1]


def test_connection_profile_store_supports_multiple_profiles_and_secret_cleanup(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'profiles.db'}")
    database.migrate()
    vault = SecretVault(tmp_path / "secrets", cipher=_Cipher())
    store = ConnectionProfileStore(SettingsStore(database), vault)

    personal = store.create(
        provider_id="example",
        display_name="Personal",
        account="person@example.com",
        granted_scopes=("read",),
        secret_values={"refresh_token": "personal-refresh"},
    )
    work = store.create(
        provider_id="example",
        display_name="Work",
        account="person@work.example",
        granted_scopes=("read", "write"),
        secret_values={"refresh_token": "work-refresh"},
    )

    assert [profile.display_name for profile in store.list("example")] == [
        "Personal",
        "Work",
    ]
    assert store.active("example") == work
    assert store.secret(personal, "refresh_token") == "personal-refresh"
    assert "personal-refresh" not in json.dumps(
        store.settings.get(ConnectionProfileStore.PROFILES_KEY)
    )
    store.set_active("example", personal.profile_id)
    assert store.active("example").profile_id == personal.profile_id

    reference = personal.secret_references["refresh_token"]
    deleted = store.delete(personal.profile_id)

    assert deleted.profile_id == personal.profile_id
    assert vault.get(reference) is None
    assert store.active("example").profile_id == work.profile_id


class _MemoryVault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.writes: list[str] = []

    def get(self, name: str):
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.writes.append(name)
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


class _Response(io.BytesIO):
    def __init__(self, payload) -> None:
        value = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        super().__init__(value)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _ProviderError(RuntimeError):
    def __init__(self, status: int, reason: str, recoverable: bool) -> None:
        self.status = status
        self.reason = reason
        self.recoverable = recoverable
        super().__init__(reason)


def _provider_error(status: int, reason: str, recoverable: bool):
    return _ProviderError(status, reason, recoverable)


def test_oauth_token_manager_commits_last_and_tracks_scopes():
    vault = _MemoryVault()
    names = OAuthSecretNames(
        client_id="example_client_id",
        client_secret="example_client_secret",
        refresh_token="example_refresh_token",
        access_token="example_access_token",
        expires_at="example_expires_at",
        account="example_account",
        granted_scopes="example_scopes",
    )
    manager = OAuthTokenManager(
        vault,
        names=names,
        token_url="https://identity.example.test/token",
        revoke_url="https://identity.example.test/revoke",
        provider_name="Example",
        clock=lambda: 1000,
        error_factory=_provider_error,
    )

    manager.save_authorization(
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "scope": "write read read",
        },
        client_id="client",
        client_secret="secret",
        account="person@example.com",
        requested_scopes=("fallback",),
    )

    assert vault.writes[-1] == names.client_id
    assert manager.connected()
    assert manager.account() == "person@example.com"
    assert manager.granted_scopes() == ("read", "write")
    assert vault.get(names.expires_at) == "4600"


def test_loopback_oauth_rejects_wrong_state_without_waiting_for_timeout():
    def authorization_url(redirect_uri: str, _pkce: PKCEAuthorization) -> str:
        return redirect_uri

    def browser_open(redirect_uri: str) -> bool:
        callback = redirect_uri + "?" + urllib.parse.urlencode({"state": "wrong", "code": "code"})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(callback, timeout=5)
        return True

    with pytest.raises(PermissionError, match="could not be verified"):
        run_loopback_authorization(
            authorization_url=authorization_url,
            exchange=lambda *_args: "unused",
            browser_open=browser_open,
            messages=OAuthLoopbackMessages(
                connected_heading="Connected",
                failed_heading="Not connected",
            ),
            error_message=lambda error: str(error),
            logger=__import__("logging").getLogger(__name__),
            timeout_seconds=30,
        )


@pytest.mark.asyncio
async def test_external_json_client_retries_bounded_reads_but_not_writes():
    class Tokens:
        async def access_token(self, *, force_refresh=False):
            return "token"

    calls = []

    def opener(request, timeout):
        calls.append(request.method)
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "Rate limited",
                {"Retry-After": "0"},
                io.BytesIO(json.dumps({"error": {"message": "Slow down"}}).encode()),
            )
        return _Response({"items": []})

    client = ExternalJsonClient(
        "https://api.example.test/v1",
        Tokens(),
        provider_name="Example",
        opener=opener,
        max_attempts=3,
        error_factory=_provider_error,
    )

    assert await client.request("GET", "/items") == {"items": []}
    assert calls == ["GET", "GET", "GET"]

    write_calls = []

    def failing_write(request, timeout):
        write_calls.append(request.method)
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "Unavailable",
            {},
            io.BytesIO(b"{}"),
        )

    writes = ExternalJsonClient(
        "https://api.example.test/v1",
        Tokens(),
        provider_name="Example",
        opener=failing_write,
        max_attempts=3,
        error_factory=_provider_error,
    )
    with pytest.raises(_ProviderError) as captured:
        await writes.request("POST", "/items", body={"name": "one"})
    assert captured.value.recoverable is True
    assert write_calls == ["POST"]


@pytest.mark.asyncio
async def test_external_json_client_refreshes_but_does_not_replay_unauthorized_write():
    class Tokens:
        def __init__(self):
            self.refreshes = []

        async def access_token(self, *, force_refresh=False):
            self.refreshes.append(force_refresh)
            return "fresh" if force_refresh else "expired"

    calls = []

    def opener(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"{}"),
        )

    tokens = Tokens()
    client = ExternalJsonClient(
        "https://api.example.test/v1",
        tokens,
        provider_name="Example",
        opener=opener,
        error_factory=_provider_error,
    )

    with pytest.raises(_ProviderError) as captured:
        await client.request("POST", "/items", body={"name": "one"})

    assert captured.value.status == 401
    assert tokens.refreshes == [False, True]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_external_json_client_blocks_unsafe_paths_and_oversized_responses():
    tokens = SimpleNamespace(access_token=lambda **_kwargs: asyncio.sleep(0, result="token"))
    client = ExternalJsonClient(
        "https://api.example.test/v1",
        tokens,
        provider_name="Example",
        opener=lambda *_args, **_kwargs: _Response(b"x" * 2049),
        max_response_bytes=2048,
        error_factory=_provider_error,
    )

    with pytest.raises(ValueError, match="safe relative"):
        await client.request("GET", "https://attacker.example/")
    for unsafe_path in (
        "/../admin",
        "/%2e%2e/admin",
        "/%2525252e%2525252e/admin",
        "/items?redirect=evil",
        "/items#part",
    ):
        with pytest.raises(ValueError, match="safe relative"):
            await client.request("GET", unsafe_path)
    with pytest.raises(_ProviderError, match="safety limit"):
        await client.request("GET", "/items")


@pytest.mark.asyncio
async def test_external_json_client_protects_security_headers_from_adapter_overrides():
    class Tokens:
        async def access_token(self, *, force_refresh=False):
            return "trusted-token"

    captured = {}

    def opener(request, timeout):
        captured.update(dict(request.header_items()))
        return _Response({})

    client = ExternalJsonClient(
        "https://api.example.test/v1",
        Tokens(),
        provider_name="Example",
        opener=opener,
        error_factory=_provider_error,
    )

    await client.request(
        "GET",
        "/items",
        headers={"Authorization": "Bearer attacker", "User-Agent": "Unreviewed"},
    )

    assert captured["Authorization"] == "Bearer trusted-token"
    assert captured["User-agent"] == "LocalAssistant/0.1 Integration"
