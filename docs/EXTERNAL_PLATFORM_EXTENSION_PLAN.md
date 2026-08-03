# External Platform Extension Roadmap

This document turns the Extension Hub roadmap into an implementation sequence.
It assumes Google Calendar and Gmail remain first-party managed extensions and
that signed catalog packages continue to run without host networking or
credentials.

## Outcome

Add useful external account integrations without weakening the existing trust
boundary:

- OAuth and API credentials stay in the host's encrypted `SecretVault`.
- Every managed extension is installed disabled and receives no permission
  grant until the user reviews it.
- Account-backed extensions enable only while their connection is usable and
  all extension-specific scopes are present.
- Read, write, send, network, and filesystem permissions remain separate and
  exact-targeted.
- Provider adapters return bounded, typed, redacted results and never expose
  tokens to skill arguments, catalog packages, logs, or model context.

## Implementation status

The current increment implements the shared binding/facade foundation plus
managed read-only slices for Google Drive, Microsoft Graph (Outlook Mail,
Outlook Calendar, and OneDrive), Slack, Notion, and GitHub. It includes
deterministic routing, connection dialogs, Hub install/restart synchronization,
offline-gated fixed-origin adapters, focused UI tests, and a full regression
run. Real provider consent/token acceptance is intentionally a release-lane
task because this repository does not contain production credentials; Slack,
Notion, and GitHub also retain their planned OAuth/App onboarding work.

## Delivery order

### Stage 0: shared connection and routing foundation

1. Make the connection dialog path scope-aware. A `ConnectionBinding` already
   knows the profile and required scopes for an extension; connection dialogs
   must pass those requirements into `connect()` so shared providers can use
   incremental consent instead of requesting a union of every product scope.
2. Add a provider-neutral bound connection facade for dialogs. It injects the
   binding's `profile_id` and `required_scopes` only when the provider accepts
   them, while preserving compatibility with the existing Calendar/Gmail
   services.
3. Add explicit provider routing helpers. Names such as `Gmail`, `Outlook`,
   `Slack`, `Notion`, or `GitHub` select a platform; generic multi-provider
   disambiguation remains a follow-up once more than one provider is enabled.
4. Keep profile persistence reusable. The first new provider slices use one
   encrypted active token per provider; multi-account/profile selection uses
   `ConnectionProfileStore` in a later increment. Existing Calendar/Gmail token
   names stay separate until an explicit migration is designed.

### Stage 1: Google Drive

Extension ID: `integration.google_drive`.

Initial read-only operations:

- search files and folders;
- list a folder's children;
- retrieve metadata for an exact file or folder;
- content export remains a separate follow-up because it needs a broader scope
  and a dedicated response-size review.

Do not implement upload, move, delete, sharing, or permission changes in the
first slice. Keep Drive tokens separate from Calendar/Gmail tokens initially.
Use metadata-read access for discovery where possible; content retrieval is a
separate scope and release review because Google classifies broad Drive reads
as restricted access.

### Stage 2: Microsoft Graph suite

Use one `microsoft_graph` connection service and separate managed entries that
share its active encrypted authorization:

- `integration.outlook_mail` — read/search first, drafts and send/reply later;
- `integration.outlook_calendar` — list/search/free-busy first, mutations later;
- `integration.onedrive` — metadata/search/read first;
- `integration.microsoft_todo` — staged after the task result model is agreed.

Consent is incremental per managed entry. Personal and organizational accounts
are supported by the provider flow; tenant identity and active-profile
selection remain explicit UI work for the profile increment.

### Stage 3: Slack

Extension ID: `integration.slack`.

Initial operations are channel discovery, bounded history/thread reads, and
message search through a user-provided encrypted read token. Confirmed sends
remain deferred. Slack's current OAuth installation flow requires a registered
HTTPS redirect for production use, so public OAuth is a follow-up after a
reviewed desktop callback relay exists. Do not start with broad message
ingestion, automatic DMs, file uploads, webhooks, or public-wide posting.

### Stage 4: Notion

Extension ID: `integration.notion`.

Start with encrypted internal-integration or personal read-token access,
search over pages and data sources shared with the connection, and bounded
page/block reads. Public OAuth and simple note creation under a selected parent
are follow-up work. Defer arbitrary database schema mapping and two-way task
synchronization.

### Stage 5: GitHub

Extension ID: `integration.github`.

Start with repository-scoped reads for repositories, issues, pull requests,
reviews, and release status. Add confirmed issue/comment writes later. Do not
touch repository secrets, workflow mutation, branch deletion, or code pushes.
The first read-only slice accepts an encrypted fine-grained personal token with
narrow repository permissions. Prefer a GitHub App with narrowly scoped
repository permissions for the follow-up authorization flow; do not touch
repository secrets, workflow mutation, branch deletion, or code pushes.

## Shared implementation rules

Each managed provider follows the existing layout:

```text
src/assistant/integrations/<provider>.py
src/assistant/skills/<provider>.py
src/assistant/router/handlers/<provider>.py       # only stable common phrases
src/assistant/ui/desktop/<provider>_connection.py # when account UI exists
tests/test_<provider>.py
tests/ui/test_<provider>_connection.py            # when account UI exists
```

Every provider must use `ManagedExtensionSpec.from_factory()` and register its
specification in the shared `ManagedExtensionRegistry`. Shared providers may
reuse one service/profile, but each extension keeps its own `required_scopes`
and manifest permissions.

Writes, sends, creates, updates, and deletes use `safe_to_retry=False` unless
the adapter has an explicit idempotency or reconciliation strategy. Network
calls use fixed HTTPS origins through `ExternalJsonClient`; provider errors,
pagination, response sizes, downloads, and model-facing result text are
bounded.

New account integrations are foreground-only or capability-gated initially.
Background sync, webhooks, and scheduled summaries are separate follow-up
work after token refresh, revocation, offline behavior, and reconnect paths
are proven.

## Definition of done for each extension

- managed catalog entry is visible without a remote catalog;
- install remains disabled until skill review;
- connection status handles disconnected, missing scopes, reauthorization,
  errors, and restart synchronization;
- offline mode blocks OAuth refresh and API calls before network access;
- read/write/send permissions produce exact, user-visible targets;
- malformed, oversized, unauthorized, rate-limited, and partial responses are
  handled deterministically;
- unit, routing, connection-dialog, Hub, and offline tests pass without real
  network access;
- `docs/FEATURES.md`, `docs/USER_GUIDE.md`, and this roadmap describe the
  supported operations and limitations;
- a manual release lane validates real OAuth consent, refresh, revoke, missing
  scopes, and at least one account/profile per provider.

## Deferred platform work

Spotify, Discord, Dropbox, Teams, and broad third-party catalog networking stay
deferred. They need separate decisions about device state, bot/user identity,
file-content scope, tenant administration, or a new reviewed host broker for
credentialed worker packages.
