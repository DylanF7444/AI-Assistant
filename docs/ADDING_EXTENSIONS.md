# Adding Extensions to the Extension Hub

This guide is for contributors adding a new Extension Hub entry. It covers the
two extension paths supported by this repository, the contracts shared by both,
the required wiring, and the tests that must pass before release.

Read `docs/EXTENSION_HUB.md` for the external catalog protocol and
`docs/SKILLS.md` for the underlying skill interface. Existing reference
implementations are Google Calendar and Gmail.

## Choose the extension path first

| Requirement | First-party managed extension | Signed catalog package |
| --- | --- | --- |
| Implementation ships in this repository | Yes | No |
| Visible without an external catalog | Yes | No |
| May use host adapters or encrypted secrets | Yes | No |
| May have an in-app account connection dialog | Yes | No |
| Runs in the main application boundary | Yes | No, runs in an isolated worker |
| Outbound network access | Through reviewed host adapters and permissions | Currently blocked in the worker |
| Installation source | Reviewed implementation in the application build | Public HTTPS ZIP from the catalog |
| Package signature | Not applicable | Trusted Ed25519 signature required |

Use a **first-party managed extension** for OAuth, API tokens, external account
connections, or another integration that needs a reviewed host service. Use a
**signed catalog package** only for a self-contained skill that needs no host
credentials or outbound network access.

An always-available core capability normally belongs in the built-in registry,
not the Extension Hub. Hub entries are optional capabilities that users choose
to install and review independently.

## Managed extension foundation

First-party extensions are composed through `ManagedExtensionSpec` and
`ManagedExtensionRegistry` in `assistant.extensions.platform`, rather than
through separate Hub conditionals for each provider. A spec is the single
reviewed record for:

- a `source="first_party"` `CatalogExtension`;
- the matching `SkillManifest`;
- a zero-argument factory that creates a fresh `Skill`; and
- an optional `ConnectionBinding`.

Build specs with `ManagedExtensionSpec.from_factory()`. It validates that the
factory returns a `Skill`, derives the catalog identity and permissions from
the manifest, creates a valid non-downloadable HTTPS placeholder URL, and
computes a manifest digest for the managed catalog entry. Construction also
checks that a fresh factory result has exactly the expected manifest and that
catalog metadata cannot drift from it. The registry rejects duplicate IDs and
provides the catalog entries, skill factories, specs, and connection bindings
from one provider-neutral source.

Bootstrap should create the registry once and share it with both composition
points:

```python
managed = ManagedExtensionRegistry(
    (
        ManagedExtensionSpec.from_factory(
            acme_skill_factory,
            package_slug="acme",
            featured=False,
            connection=ConnectionBinding(
                service=acme_connection,
                provider_id=acme_connection.provider_id,
                profile_id=profile_id,
                required_scopes=(ACME_READ_SCOPE,),
                dialog_entry_point=(
                    "assistant.ui.desktop.acme_connection:AcmeConnectionDialog"
                ),
            ),
        ),
    )
)

registry = SkillRegistry(
    settings.data_dir / "skills",
    managed_skill_factories=managed.skill_factories,
)
extension_hub = ExtensionHubService(
    registry,
    client,
    audit=audit,
    managed_registry=managed,
)
```

Keep the same stable skill ID in the manifest, factory, managed spec, catalog,
and any routing. The binding's `provider_id` must match the connection
service's provider namespace, but may intentionally differ from the extension
ID. The managed catalog entry is merged with remote catalog entries and wins
on an ID collision. Installation persists the manifest and loads the shipped
factory without downloading a package; it is still initially disabled. On
startup, `load_all_installed()` recreates managed skills through their factories
and `sync_connected_extensions()` enables only installed account-backed skills
whose binding is usable.

## Contracts every extension must preserve

Each extension is a skill and therefore needs:

- A stable lowercase ID matching `^[a-z][a-z0-9_.-]+$`. First-party
  integrations use `integration.<name>`.
- Pydantic input and output models. Use `extra="forbid"` for inputs so planner
  mistakes and untrusted fields fail closed.
- A `SkillManifest` targeting API `1.0`. Its input and output schemas must
  exactly match the implementation models.
- Complete declared permissions. Resolve narrower per-call targets in
  `required_permissions()` when the operation or resource is known only at
  runtime.
- A conservative manifest risk plus `risk_for()` when different operations
  have different consequences.
- Bounded `SkillResult` data. Mark account data, messages, documents, or other
  private content as `sensitive=True`.
- Cancellation checks before work and during bounded loops where practical.
- No credentials in skill arguments, results, catalog data, audit details,
  exceptions, or language-model context.

Choose `background_mode` deliberately. `foreground_only` is safest.
`capability_gated` is appropriate only after reviewing the background policy
and every resolved capability. Do not mark a skill `safe_to_retry=True` if an
operation can send, create, delete, charge, publish, or otherwise duplicate an
external side effect.

### Minimal skill shape

Managed skills can receive a reviewed adapter through their constructor:

```python
from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, ConfigDict

from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest


class AcmeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["list_items", "create_item"]
    item_id: str | None = None
    title: str | None = None


class AcmeOutput(BaseModel):
    operation: str
    resources: list[dict]


class AcmeSkill(Skill):
    input_model = AcmeInput
    output_model = AcmeOutput

    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.manifest = manifest(
            skill_id="integration.acme",
            display_name="Acme",
            description="Read and manage approved Acme items.",
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="integration.read",
                    reason="Read approved Acme items.",
                ),
                PermissionScope(
                    capability="integration.write",
                    reason="Change approved Acme items.",
                ),
                PermissionScope(
                    capability="network",
                    targets=("https://api.acme.example",),
                    reason="Connect to the Acme API.",
                ),
            ),
            risk=RiskLevel.HIGH,
            safe_to_retry=False,
            background_mode=BackgroundMode.CAPABILITY_GATED,
        ).model_copy(update={"entry_point": f"{__name__}:AcmeSkill"})

    def risk_for(self, call: SkillCall) -> RiskLevel:
        arguments = self.validate(call.arguments)
        return RiskLevel.LOW if arguments.operation == "list_items" else RiskLevel.HIGH

    def required_permissions(self, call: SkillCall):
        arguments = self.validate(call.arguments)
        capability = (
            "integration.read"
            if arguments.operation == "list_items"
            else "integration.write"
        )
        target = f"acme:item:{arguments.item_id or '*'}"
        return (
            PermissionScope(
                capability=capability,
                targets=(target,),
                reason=f"Allow {arguments.operation} on this exact Acme target.",
            ),
            PermissionScope(
                capability="network",
                targets=("https://api.acme.example",),
                reason="Connect to the Acme API.",
            ),
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        arguments = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="Acme action cancelled.")
        payload = await self.adapter.execute(arguments)
        output = AcmeOutput(operation=arguments.operation, resources=payload)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Acme operation completed.",
            data=output.model_dump(mode="json"),
            sensitive=True,
        )
```

Model validators should enforce operation-specific required fields. The
adapter must accept already validated, structured data; it must never construct
code, URLs, paths, or shell commands directly from untrusted free-form text.

## Adding a first-party managed extension

### 1. Create the implementation modules

Use this layout when the corresponding concern exists:

```text
src/assistant/integrations/acme.py          # API, token, and connection services
src/assistant/skills/acme.py                # typed skill contract and operation mapping
src/assistant/router/handlers/acme.py       # optional deterministic common commands
src/assistant/ui/desktop/acme_connection.py # optional account dialog
tests/test_acme.py
tests/ui/test_acme_connection.py            # when a dialog exists
```

Keep OAuth and API credentials in `SecretVault`. Give every provider its own
secret names so disconnecting one extension cannot break another. The skill
may receive an adapter that uses the vault, but credentials must never become
skill input fields or result data. If several extensions share one provider
authorization, give them the same connection service and `profile_id`, then
declare each extension's own `required_scopes` in its `ConnectionBinding`.

An account connection service used by `ConnectionBinding` must provide a
provider-neutral status surface plus health testing and disconnect:

```python
def status(
    self,
    *,
    profile_id=None,
    required_scopes: tuple[str, ...] = (),
):
    # Return ConnectionStatus, or a legacy-compatible object with at least
    # connected/account/healthy/message fields.
    ...

async def connect(self, client_id: str | None = None): ...
async def test(self): ...
async def disconnect(self): ...
```

`ConnectionStatus` normalizes granted and required scopes and can report
`disconnected`, `connected`, `missing_scopes`, `reauthorization_required`, or
`error`. `usable` is true only for `connected` with no missing scopes;
`authenticated` may still be true for `missing_scopes`. The Hub disables the
skill for every other state and reports reauthorization guidance for missing
scopes, provider reauthorization, and errors. Keep `healthy` separate from
authorization state so a health-check failure does not masquerade as a new
account connection.

Disconnect should revoke authorization when the provider supports it and
delete account tokens. Application-level OAuth client configuration may be
retained when it is not a user credential. `status().connected` must use a
durable commit marker, such as the presence of both the client ID and refresh
token, rather than an in-memory flag. A binding can pass `profile_id` and
`required_scopes` to a status method that accepts them; services with the old
no-argument status method remain supported.

### 2. Add the managed factory, binding, and Hub metadata

In `assistant.bootstrap`, add a factory that creates a fresh implementation,
one provider connection service, and one `ManagedExtensionRegistry` entry. Let
`ManagedExtensionSpec.from_factory()` derive the catalog entry from the
manifest instead of hand-maintaining duplicate metadata:

```python
def _acme_skill_factory(secret_vault: SecretVault):
    return lambda: AcmeSkill(AcmeAdapter(AcmeTokenManager(secret_vault)))


def _managed_extension_registry(secret_vault: SecretVault):
    acme_factory = _acme_skill_factory(secret_vault)
    acme_connection = AcmeConnectionService(secret_vault)
    return ManagedExtensionRegistry(
        (
            ManagedExtensionSpec.from_factory(
                acme_factory,
                package_slug="acme",
                featured=False,
                connection=ConnectionBinding(
                    service=acme_connection,
                    provider_id=acme_connection.provider_id,
                    required_scopes=(ACME_SCOPE,),
                    dialog_entry_point=(
                        "assistant.ui.desktop.acme_connection:AcmeConnectionDialog"
                    ),
                ),
            ),
        )
    )


managed_extensions = _managed_extension_registry(storage.secret_vault)
registry = SkillRegistry(
    settings.data_dir / "skills",
    managed_skill_factories=managed_extensions.skill_factories,
)

extension_hub = ExtensionHubService(
    registry,
    client,
    managed_registry=managed_extensions,
)
```

Merge the new spec with the existing tuple; do not replace Google Calendar or
Gmail. The factory, catalog entry, manifest, and any routing must use the exact
same stable extension ID. The binding's `provider_id` must match the connection
service's shared provider namespace; `profile_id` may select one
account/workspace when a provider supports multiple profiles.
`required_scopes` is the per-extension scope contract and should be explicit
even when the provider service has a default scope set.

At startup, `registry.load_all_installed()` recreates persisted managed skills
through the factory. `extension_hub.sync_connected_extensions()` enables an
installed account-backed skill only while its connection binding reports
`usable`. Every new installation is initially registered disabled. Connecting
an account synchronizes and enables an account-backed extension; disconnecting,
missing scopes, reauthorization, or a connection error disables it. A managed
extension with no connection service is enabled manually from **Skills** after
review.

### 3. Wire an account dialog when needed

Create a dialog following the Google Calendar or Gmail connection dialog. It
should:

- Explain the requested provider scope before sign-in.
- Disable controls while asynchronous work is running.
- Support connect, health test, disconnect, and provider-specific recovery.
- Emit `connection_changed` after connect or disconnect.
- Never display an access token, refresh token, client secret, or raw provider
  error body.

Put the dialog factory in the binding's `dialog_entry_point` using
`module:Object` syntax. The Hub dynamically resolves that provider-neutral
entry point, calls it with the connection service and parent widget, checks for
`connection_changed` and `exec`, and connects the signal to its existing
`_sync_connection_state()` path. Do not add a provider-specific conditional to
`ExtensionHubPage.configure_connection()`. The Hub already hides the account
button until the extension is installed and disables it under the global
offline lock.

### 4. Reuse profiles and provider transport helpers

Use `ConnectionProfileStore` when a provider can have multiple accounts,
workspaces, or endpoints. It persists validated profile metadata in the
application settings store and keeps all credential values in the encrypted
`SecretVault`. A `ConnectionProfile` carries a UUID, provider ID, display name,
optional account and endpoint, JSON-safe configuration, secret references,
granted scopes, and timestamps. Create profiles with `secret_values` so the
store creates profile-scoped references; never put token values in
`configuration` or other profile metadata. Use the active-profile methods when
selecting a connection, and use `secret(profile, logical_name)` only inside the
reviewed provider service. Deleting a profile removes its referenced secrets;
provider revocation remains the service's responsibility.

`bootstrap.py` creates one `ConnectionProfileStore` with `SettingsStore` and
`SecretVault` and exposes it on the application container. The direct Gmail,
Google Calendar, and Google Drive service classes remain backward-compatible for
callers that construct them explicitly. The desktop Hub composes the Google
entries through one shared provider service and common encrypted authorization,
with each binding retaining its own required scopes. A new profile-aware
provider can adopt the store without changing those direct integration APIs.

For desktop OAuth, use `run_loopback_authorization()` and
`OAuthTokenManager` from `assistant.integrations.oauth`:

- The loopback helper binds an ephemeral port on `127.0.0.1`, uses state and
  S256 PKCE, accepts only the expected callback path and state, bounds the
  timeout to 30–600 seconds (180 seconds by default), and always shuts down
  the listener.
- The token manager validates HTTPS endpoints, stores tokens/account/scopes in
  `SecretVault`, refreshes expiring access tokens, bounds OAuth responses, and
  writes the client ID last as the durable connection commit marker. OAuth
  responses are limited to 1 MiB by default.
- Provider error bodies and callback messages must be reduced to safe bounded
  text. Disconnect should revoke when supported and delete authorization data,
  while retaining non-user application configuration when appropriate.

For JSON APIs, use `ExternalJsonClient` from
`assistant.integrations.http`. Give it one fixed HTTPS origin and use only
safe relative paths. It supplies the bearer token, bounds response and error
body sizes (2 MiB and 64 KiB by default), rejects malformed response objects,
and performs one forced token refresh after a 401. It replays the request only
when the method is idempotent or the adapter explicitly opts in. Do not
construct arbitrary origins from user input.

Retry safety is part of the skill contract. The client retries only
`GET`/`HEAD`/`OPTIONS` by default; transient network failures and HTTP 408,
429, 500, 502, 503, and 504 are bounded by the configured attempt and backoff
limits. Pass `safe_to_retry=False` for any send/create/update/delete operation
whose replay could duplicate a side effect. A 401 refresh is an authentication
recovery path separate from transient retries; non-repeatable operations are
not replayed automatically after that refresh.

### 5. Add deterministic routing only for stable common commands

Planner retrieval uses the manifest description automatically. Add a route
handler only for high-confidence phrases that can be mapped to a complete typed
request without guessing.

The handler should call `router.skill_availability(<skill_id>)` and return
specific setup guidance for `unavailable`, `disabled`, `disconnected`, and
`reauthorization_required` states. Add the handler to
`DEFAULT_ROUTE_HANDLERS` before the general application and fallback handlers.
Do not intercept phrases that should still open a website or desktop
application.

### 6. Document the user-facing setup

For every managed extension:

- Add a row to `docs/FEATURES.md` with status, risk, permissions, and scope.
- Add account setup, supported operations, and explicit limitations to
  `docs/USER_GUIDE.md`.
- Update `docs/EXTENSION_HUB.md` if the extension introduces a new platform or
  trust-boundary pattern.
- Name provider prerequisites, such as enabling an API or configuring an OAuth
  consent screen, without committing credentials or machine-specific values.

### Compatibility during migration

`ExtensionHubService` still accepts the earlier
`managed_extensions`, `connection_services`, and `connection_dialogs`
constructor arguments. Use the new `managed_registry` argument for new work;
the old arguments are wrapped into managed specs only when a registry is not
provided. Do not pass both forms. The compatibility wrapper uses a service's
declared provider ID when available, otherwise it falls back to the extension
ID; it cannot add per-profile or per-extension required scopes unless the
service itself supplies them.

`ConnectionStatus.coerce()` adapts older status objects that expose the
original `connected`, `account`, `healthy`, and `message` fields. A binding
inspects the `status()` signature and supplies `profile_id` and
`required_scopes` only when the service supports those keyword arguments.
This keeps the direct Google Calendar, Gmail, and Google Drive connection
services and dialogs working while new providers adopt the shared status model.
The desktop Hub performs a guarded migration from their old token names into
the shared Google authorization only when legacy account identity is
unambiguous; do not silently merge different accounts.

## Adding a signed catalog package

Catalog packages are loaded by `assistant.skills.worker`. The entry-point class
must have a no-argument constructor because the worker creates it as
`ClassName()`. The worker receives only a small allowlist of operating-system
environment variables, does not inherit parent secrets, has bounded runtime and
memory, and currently forces outbound networking off. Loopback is the only
network exception.

The Python process and Windows Job Object controls are defense in depth, not a
complete hostile-code sandbox. Only approve packages from reviewed publishers
whose public signing keys were provisioned through a trusted build or
configuration path.

### 1. Build the package directory

```text
acme-package/
  manifest.json
  plugin.py
```

The module named by `entry_point`, such as `plugin:AcmeSkill`, must exist at the
corresponding package path. Generate `manifest.json` from the implementation so
the schemas cannot drift:

```python
from pathlib import Path

from plugin import AcmeSkill

skill = AcmeSkill()
Path("manifest.json").write_text(
    skill.manifest.model_dump_json(indent=2),
    encoding="utf-8",
)
```

Do not include private keys, tokens, local configuration, virtual environments,
model files, generated installers, or unnecessary dependencies.

### 2. Sign with the publisher key

Keep the private key outside the repository and build output. The application
trust store contains only the matching raw 32-byte Ed25519 public key, encoded
as base64 under its stable key ID.

```python
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from assistant.skills.package import sign_package

private_key = serialization.load_pem_private_key(
    Path("outside-repository/publisher-private.pem").read_bytes(),
    password=None,
)
if not isinstance(private_key, Ed25519PrivateKey):
    raise TypeError("Expected an Ed25519 private key")

sign_package(Path("acme-package"), "acme-2026", private_key)
```

This creates `signature.json`. Sign only after every other package file is
final. Any later change correctly invalidates the signature.

Validate with a test-local registry configured with the matching public key:

```python
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from assistant.skills.registry import SkillRegistry

public_key = serialization.load_pem_public_key(
    Path("publisher-public.pem").read_bytes()
)
if not isinstance(public_key, Ed25519PublicKey):
    raise TypeError("Expected an Ed25519 public key")
raw_public_key = public_key.public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)

registry = SkillRegistry(trusted_signing_keys={"acme-2026": raw_public_key})
registry.validate_package(Path("acme-package"))
```

Production `trusted_skill_keys` values are base64 encodings of
`raw_public_key`. A key named by catalog content is never trusted automatically.

### 3. Archive and publish

Create a ZIP containing the package files at its root or inside one top-level
directory. The final archive must contain `manifest.json`, `signature.json`,
and the entry-point module. Compute SHA-256 after creating the final ZIP:

```powershell
Compress-Archive -Path acme-package\* -DestinationPath acme-1.0.0.zip
(Get-FileHash acme-1.0.0.zip -Algorithm SHA256).Hash.ToLowerInvariant()
```

Publish the immutable ZIP at a public HTTPS URL. The host must resolve only to
global addresses; redirects to private, local, or reserved targets are blocked.

### 4. Add the catalog entry

Copy identity and security metadata exactly from `manifest.json`:

- `extension_id` from `skill_id`
- `version`, `api_version`, `display_name`, `description`, and `category`
- the complete ordered `permissions` value
- `risk`
- `package_sha256` from the final ZIP

Set `source` only for application-owned managed extensions. Normal catalog
entries omit it and default to `platform`. Refresh the Hub against the published
catalog, install, confirm that the skill is disabled, then enable and exercise
it through the normal permission flow.

## Required regression coverage

Add focused tests before running the full suite.

### Skill behavior

- Input rejects missing fields, unknown fields, unsafe identifiers, and
  ambiguous changes.
- Every operation resolves the expected risk and exact permission targets.
- Adapter method, path, query, and body mapping are exact and bounded.
- Results validate against the output model and mark private data sensitive.
- Cancellation and empty-result messages behave predictably.
- Send/create/delete operations are not marked safe to retry.

### Managed extension lifecycle

- The entry is visible with no external catalog configured.
- `ManagedExtensionSpec` rejects a non-first-party source, a factory that does
  not return a `Skill`, manifest drift, catalog metadata drift, and invalid
  package slugs.
- `ManagedExtensionRegistry` rejects duplicate IDs and exposes the same
  factories, catalog entries, and connection bindings used by bootstrap.
- It is not registered before installation.
- Installation persists the managed marker and registers the skill disabled.
- A shared service/profile can back multiple extensions while each binding's
  required scopes are evaluated independently.
- `disconnected`, `missing_scopes`, `reauthorization_required`, and `error`
  states disable the account-backed skill; only a usable connected status
  enables it.
- A connected installed extension is enabled again after restart.
- Disconnect removes account tokens and disables the skill.
- Legacy Hub constructor arguments and older status objects continue to map to
  the new registry/status model.
- Profile metadata, active-profile selection, profile-scoped secret references,
  deletion cleanup, OAuth state/PKCE, token refresh, bounded provider errors,
  and secret separation are tested when applicable.

### Provider transport and retry behavior

- Loopback OAuth binds only to local ephemeral addresses, validates state and
  PKCE, bounds the callback, and always closes its listener.
- Fixed-origin clients reject unsafe origins and paths, bound JSON/error
  bodies, refresh once after a 401, and redact provider failures.
- Default retries cover only idempotent methods; side-effecting calls opt out
  with `safe_to_retry=False`, and the authentication replay behavior is tested
  separately.

### Package lifecycle

- A signed package validates with the intended public key.
- Tampering, unsigned packages, unknown keys, digest mismatch, catalog metadata
  mismatch, unsafe archive paths, and incompatible API versions fail closed.
- The no-argument entry point executes through the isolated worker.
- Timeout, cancellation, blocked outbound networking, and absence of inherited
  parent secrets remain covered.

### Router and UI

- Common phrases route to valid input models without an LLM.
- Unavailable, disabled, disconnected, and reauthorization-required states give
  actionable guidance.
- Similar website/application-launch phrases are not intercepted.
- Provider-neutral dialog entry points resolve to a factory with
  `connection_changed` and `exec`; dialogs cover success, failure, advanced
  credentials when supported, health testing, and confirmed disconnect.

Suggested commands:

```powershell
python -m ruff check src/assistant/integrations/acme.py src/assistant/skills/acme.py tests/test_acme.py
python -m pytest tests/test_acme.py -q
python -m pytest tests/test_extension_hub.py tests/test_skill_isolation.py -q
python -m pytest -m ui tests/ui/test_acme_connection.py tests/ui/test_extension_hub_page.py -q
python -m pytest
```

## Versioning and release checklist

Before release, verify:

- [ ] The extension ID is unique and stable.
- [ ] The manifest and catalog metadata agree exactly.
- [ ] Permissions, targets, risk, retry behavior, and background mode were
      reviewed operation by operation.
- [ ] Connection provider/profile identity and required scopes are explicit;
      shared profiles do not leak secrets across providers.
- [ ] No secret can cross into arguments, results, logs, catalog JSON, or model
      context.
- [ ] OAuth uses the loopback/PKCE helpers and API calls use fixed approved
      HTTPS origins, bounded responses, and honor offline mode.
- [ ] User documentation describes setup, supported operations, and omissions.
- [ ] Targeted, lifecycle, UI, isolation, and full regression tests pass.
- [ ] Build artifacts, credentials, private keys, and local configuration are
      absent from the change.

Do not silently change a managed extension manifest after users have installed
it. On restart, the persisted manifest is compared exactly with the factory's
current implementation; a mismatch prevents loading. The current Hub supports
install and uninstall but does not provide a managed in-place migration flow.
Plan and test an explicit migration/update path before changing manifest
schemas, permissions, risk, or other persisted metadata for a released managed
extension.

For a packaged skill update, increment the version and treat any newly declared
capability as requiring renewed review. Do not reuse a published package URL
for different bytes; publish an immutable new ZIP and matching catalog digest.

## Reference map

- Hub models, verification, installation, and audit:
  `src/assistant/extensions/platform.py`
- Managed connection bindings and normalized status:
  `src/assistant/integrations/connections.py`
- Shared loopback OAuth and token lifecycle:
  `src/assistant/integrations/oauth.py`
- Fixed-origin JSON transport and retry policy:
  `src/assistant/integrations/http.py`
- Profile metadata and SecretVault references:
  `src/assistant/storage/connections.py`
- Skill contracts: `src/assistant/contracts.py` and
  `src/assistant/skills/base.py`
- Managed/package registry lifecycle: `src/assistant/skills/registry.py`
- Package digest and Ed25519 signature format:
  `src/assistant/skills/package.py`
- Isolated worker boundary: `src/assistant/skills/subprocess.py` and
  `src/assistant/skills/worker.py`
- Application composition: `src/assistant/bootstrap.py`
- Hub UI and connection dispatch: `src/assistant/ui/desktop/extension_hub.py`
- Managed examples: `src/assistant/skills/google_calendar.py` and
  `src/assistant/skills/gmail.py`
- Package and managed lifecycle tests: `tests/test_extension_hub.py` and
  `tests/test_skill_isolation.py`
