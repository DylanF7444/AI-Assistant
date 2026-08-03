# Extension Hub and Platform Contract

For a contributor walkthrough covering first-party managed integrations,
signed catalog packages, connection services, registration, tests, and release
checks, see `docs/ADDING_EXTENSIONS.md`.

The desktop **Extension Hub** browses a configured HTTPS catalog and installs
compatible signed skill packages. Configure the catalog URL in **Settings**,
disable the global offline lock, restart the application, and open the hub.

Discovery does not grant authority. The catalog can describe a package, but it
cannot add a trusted signing key, enable a skill, or grant permissions. A newly
installed skill is disabled and receives no permission grants. Review and
enable it separately in **Skills**. An installed catalog entry can be removed
from its Hub detail page with **Uninstall Extension**; removal disables the
skill, clears its runtime registration, deletes its local package, and leaves
the entry available for a future reinstall.

## Catalog API

The configured URL is an HTTPS `GET` endpoint returning at most 2 MiB of UTF-8
JSON. Catalog schema `1.0` has this form:

```json
{
  "schema_version": "1.0",
  "extensions": [
    {
      "extension_id": "publisher.example",
      "extension_type": "skill",
      "version": "1.0.0",
      "api_version": "1.0",
      "display_name": "Example Extension",
      "description": "Perform an example operation.",
      "publisher": "Example Publisher",
      "category": "Productivity",
      "package_url": "https://cdn.example.com/publisher.example-1.0.0.zip",
      "package_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "package_format": "zip",
      "permissions": [
        {
          "capability": "network",
          "targets": [],
          "reason": "Reach the approved service."
        }
      ],
      "risk": "low",
      "featured": false
    }
  ]
}
```

This build displays every valid entry but installs only `skill` extensions
targeting core API `1.0`. Unsupported types and versions remain visible and
have a disabled install control.

## Package requirements

`package_url` must be public HTTPS. Downloads are bounded by
`extension_package_max_bytes`, which defaults to 25 MiB. The package is a ZIP
archive containing one skill package at its root or in one top-level folder:

```text
manifest.json
signature.json
plugin.py
```

Installation verifies, in order:

1. Download size and catalog SHA-256 digest.
2. Archive file count, expanded size, paths, and absence of symbolic links.
3. The trusted Ed25519 package signature.
4. Core API compatibility and manifest schema.
5. Exact agreement between catalog and manifest identity, version, display
   metadata, permissions, and risk.
6. Successful loading into the isolated skill worker boundary.

Any failure removes the staged or partially installed package and records a
local audit event. Package signer keys come from the build's
`trusted_skill_keys` configuration. Keys supplied by catalog content are never
trusted. Publisher-key additions and revocations therefore require a trusted
application configuration or build update.

The platform does not receive permission grants, execution history, local
files, or credentials from the hub. Installed skill execution continues through
the normal local schema, permission, confirmation, audit, timeout, and offline
controls described in `docs/SKILLS.md` and `docs/PERMISSIONS.md`.

Catalog packages currently execute with outbound networking forced off and do
not inherit application secrets. Use the first-party managed path for an API or
account integration that needs host networking or encrypted credentials. The
worker process and Windows resource bounds are defense in depth, not a complete
security boundary for arbitrary hostile native code; catalog publishers must
remain explicitly trusted and reviewed.

## First-party managed extensions

The Hub can also list optional first-party integrations shipped with the
application. These entries remain installable when no external catalog is
configured. Installing one persists its reviewed manifest and makes the shipped
host implementation available disabled; it does not execute downloaded package
code. Community packages continue to use the isolated worker boundary.

First-party account connections are configured from the extension's Hub detail
page. OAuth tokens remain in the host Windows-encrypted secret vault and are
never passed to either first-party extension arguments or community package
workers. Google Calendar and Gmail use this path with separate account tokens
and API scopes, so either extension can be connected or disconnected
independently.

The current bootstrap bindings are:

| Managed skill | Provider ID | Required scope | Dialog entry point |
| --- | --- | --- | --- |
| `integration.google_calendar` | `google_calendar` | `CALENDAR_SCOPE` (`https://www.googleapis.com/auth/calendar`) | `assistant.ui.desktop.google_calendar_connection:GoogleCalendarConnectionDialog` |
| `integration.gmail` | `gmail` | `GMAIL_SCOPE` (`https://www.googleapis.com/auth/gmail.modify`) | `assistant.ui.desktop.gmail_connection:GmailConnectionDialog` |

## Managed extension composition

The application-owned path is assembled from one provider-neutral
`ManagedExtensionRegistry`. Each `ManagedExtensionSpec` combines:

- the `CatalogExtension` metadata shown in the Hub;
- the matching `SkillManifest`;
- a zero-argument factory that creates a fresh reviewed `Skill`; and
- an optional `ConnectionBinding` for an account-backed extension.

`ManagedExtensionSpec.from_factory()` derives the catalog identity, version,
API version, display metadata, permissions, and risk from the factory's
manifest. It also creates a syntactically valid HTTPS placeholder package URL
and a digest of the manifest JSON. A managed entry has `source="first_party"`;
the placeholder is never downloaded. Construction verifies that the factory
returns a `Skill`, that a fresh instance has the expected manifest, and that
the catalog metadata agrees with that manifest.

`ManagedExtensionRegistry` rejects duplicate IDs and exposes the same reviewed
specifications as `catalog_extensions`, `skill_factories`, and connection
lookups. `bootstrap.py` creates the registry once, passes its factories to
`SkillRegistry`, and passes the registry to `ExtensionHubService`. This keeps
the catalog entry, persisted installation, runtime factory, and dialog
metadata on one stable extension ID. The binding separately requires its
`provider_id` to match the connection service's provider namespace; that ID
may intentionally differ from the extension ID. Google Calendar and Gmail are
the current managed entries in this registry.

Managed entries are merged into the remote catalog and take precedence over a
remote entry with the same ID. Installing a managed entry persists its
manifest and loads the shipped factory; it does not download a package or
cross the catalog-package worker boundary. As with a catalog package, a new
installation is disabled until the normal enablement and permission review
flow completes. An account-backed managed entry is enabled only while its
connection is usable, including after startup synchronization.

## Connections, profiles, and scopes

`ConnectionBinding` is the boundary between an extension and a provider
connection service. It contains:

| Field | Meaning |
| --- | --- |
| `service` | Provider-owned service implementing status, health testing, and disconnect; the service owns OAuth and credentials. |
| `provider_id` | Stable provider/account namespace, such as `gmail` or `google_calendar`. |
| `profile_id` | Optional `ConnectionProfile` selected for this extension. Bindings may point several extensions at the same service/profile. |
| `required_scopes` | The scopes this extension needs; they are normalized and evaluated in addition to the provider's granted scopes. |
| `dialog_entry_point` | Optional `module:Object` factory for the account dialog. |

Sharing a service and profile is intentional: several extensions can reuse
one provider authorization while each binding declares its own required
scopes. The binding passes `profile_id` and `required_scopes` to services that
accept them, then normalizes the result into `ConnectionStatus`. A service
with the original no-argument `status()` method remains valid. Its
`connection_key` is the `(provider_id, profile_id)` pair, so two extensions
that share both values address the same provider profile.

`ConnectionStatus` is the provider-neutral status consumed by the Hub and UI.
Its states are:

| State | Meaning | Hub behavior |
| --- | --- | --- |
| `disconnected` | No usable authorization is committed. | Reports `disconnected` and keeps the connection-backed skill disabled. |
| `connected` | Authorization exists and all required scopes are present. | `usable` is true, so an installed skill may be enabled. |
| `missing_scopes` | The account is authenticated but has not granted every required scope. | Reports `reauthorization_required`; the skill remains disabled. |
| `reauthorization_required` | The provider requires a new authorization flow. | Reports `reauthorization_required`; the skill remains disabled. |
| `error` | The connection service cannot currently use the authorization and needs recovery. | Reports `reauthorization_required`; the skill remains disabled. |

`granted_scopes` and `required_scopes` are sorted and de-duplicated. The
`missing_scopes` property is the set difference. `authenticated` is true for
both `connected` and `missing_scopes`, while `usable` is true only for the
`connected` state with no missing scopes. `healthy` is an optional result of a
provider health check and is separate from authorization state. The concrete
`connected` field remains available for older Calendar/Gmail status consumers.

`ExtensionHubService.sync_connection_state()` enables an installed managed
skill only when its binding is usable; all other connection states disable it.
`sync_connected_extensions()` applies the same rule at startup. The public
availability result preserves ordinary `enabled`, `disabled`, and
`unavailable` registry states and adds `disconnected` or
`reauthorization_required` guidance for account-backed skills.

`ConnectionProfileStore` is the reusable persistence layer for providers that
need more than one account, workspace, or endpoint. It stores validated,
non-secret `ConnectionProfile` metadata in `SettingsStore` under the versioned
profile and active-profile keys, while all credential values remain in the
encrypted `SecretVault`. A profile contains a UUID, provider ID, display name,
optional account and endpoint, JSON-safe provider configuration, secret
references, granted scopes, and timestamps. Secret references are scoped to
the profile (`integration-<profile-id.hex>-<logical-name>`) and logical names
must match the store's lowercase 64-character limit; secret values never
appear in the serialized profile.

The store supports listing and lookup, creating or saving a profile, selecting
the active profile per provider, reading one logical secret, and deleting a
profile. Deletion removes the profile's referenced secrets and selects another
profile for that provider when available; provider authorization revocation is
still the connection service's responsibility. The active profile map wins
when valid, otherwise the first enabled profile is used. `bootstrap.py` creates
one store with the application settings store and secret vault and exposes it
on the application container. Existing Gmail and Google Calendar connections
retain their provider-specific secret names and current independent behavior;
new profile-aware providers can adopt the shared store incrementally.

## Provider-neutral connection dialogs

The Hub does not contain provider-specific dialog conditionals. A binding's
`dialog_entry_point` must use `module:Object` syntax. The Hub resolves that
entry point, calls the object with the connection service and parent widget,
and requires a dialog with `connection_changed` and `exec` members. The signal
causes the Hub to resynchronize the extension state, and the Hub also
resynchronizes after the dialog closes.

The account button is shown only when the extension is installed and both a
connection service and dialog entry point are present. The global offline lock
disables it. A new provider therefore registers its service, required scopes,
profile selection, and dialog entry point in its `ConnectionBinding`; it does
not require a new provider branch in the Hub UI. The current bindings point to
the Google Calendar and Gmail dialog entry points, preserving those dialogs as
the reference implementations.

## Reusable OAuth and HTTP clients

First-party integrations should build on the shared host helpers rather than
implementing a second credential or transport path.

`run_loopback_authorization()` starts a temporary listener on `127.0.0.1` with
an ephemeral port and a fixed callback path. It generates an unpredictable
state and S256 PKCE verifier/challenge, includes the state in the authorization
request, accepts only the matching callback path and state, and passes the code
and verifier to the provider exchange callback. The browser result is a small
escaped success/failure page; provider error text is bounded and sanitized.
The default callback path is `/oauth/callback`, the default timeout is 180
seconds, the effective timeout is clamped to 30–600 seconds, and the listener
always shuts down.
`OAuthTokenManager` validates HTTPS token and revoke endpoints, exchanges codes,
refreshes expiring access tokens, records granted scopes, and keeps all token,
account, and client configuration values in `SecretVault`. The client ID and
refresh token form the durable connection marker; authorization data is written
before the client ID is committed. Revocation is best effort, authorization
secrets are deleted, and application configuration may remain for reconnect.
OAuth responses are bounded to 1 MiB by default.

`ExternalJsonClient` is a bounded fixed-origin client for reviewed provider
adapters. Its base URL must be HTTPS without credentials, query, or fragment;
requests may use only safe relative paths. It adds the bearer token, bounds
response and error bodies (2 MiB and 64 KiB by default), limits timeouts,
accepts JSON objects only, and returns bounded provider-safe errors. It is
distinct from the catalog client, which additionally requires public global
hosts and safe redirects.

Retry behavior is explicit:

- By default, only `GET`, `HEAD`, and `OPTIONS` are retryable.
- An adapter may pass `safe_to_retry=True` or `False` per request, but must use
  `False` for send/create/update/delete or other operations whose replay could
  duplicate a side effect. Provider idempotency keys or operation-specific
  reconciliation are still the adapter's responsibility.
- Transient network failures and HTTP 408, 429, 500, 502, 503, and 504 are
  retried within the bounded attempt and backoff limits. `Retry-After` is
  capped, and the default is three attempts.
- A 401 triggers at most one forced access-token refresh. Idempotent requests,
  or calls explicitly marked `safe_to_retry=True`, are replayed once with the
  fresh token. Side-effecting requests refresh the cached token but are not
  replayed automatically.

Oversized, malformed, or otherwise non-recoverable responses are not retried.
External adapters must keep credentials out of skill arguments, results,
catalog metadata, audit details, logs, and model context.

## Backward compatibility

The new registry is the preferred composition API, but the Hub still accepts
the earlier `managed_extensions`, `connection_services`, and
`connection_dialogs` constructor arguments. When `managed_registry` is not
provided, the Hub wraps those values into `ManagedExtensionSpec` objects using
the factories already registered with `SkillRegistry`. The old and new forms
must not be supplied together; unknown connection metadata and factory or
manifest mismatches fail closed.

Connection compatibility is also deliberate. `ConnectionStatus.coerce()` can
adapt an older status object that only exposes `connected`, `account`,
`healthy`, and `message`, deriving the new state and scope fields. A
`ConnectionBinding` inspects the service's `status()` signature and passes
profile and scope requirements only when supported. Existing Gmail and Google
Calendar services therefore keep their established status methods, token
names, OAuth dialogs, and independent account lifecycle while participating in
the shared Hub lifecycle. New integrations should target the registry and
binding APIs directly; compatibility support is for incremental migration, not
a reason to duplicate provider-specific Hub wiring.
