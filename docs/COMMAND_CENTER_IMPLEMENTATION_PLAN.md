# Command Center Implementation Plan

## Status

Planning baseline created August 19, 2026.

| Phase | Status |
| --- | --- |
| 0. Safety and compatibility baseline | Complete |
| 1. Command-center domain foundation | Complete |
| 2. Source and destination catalog | Complete |
| 3. Connections user interface | Complete |
| 4. Manual Work Brief | Complete |
| 5. Work artifacts and reviewed destinations | Complete |
| 6. Scheduled briefings and pending delivery | Complete |
| 7. Typed integration observations | Complete |
| 8. Hardening, documentation, and release verification | Complete |

This document is both the architectural plan and the implementation gate for
turning Local Assistant into a command center for the user's work. A phase may
start only after the preceding phase's tests and exit criteria pass. Update the
status table and implementation notes as slices land.

### Implementation notes — August 19, 2026

- Phase 5 added schema-backed, revisioned, retention-bounded artifacts; exact
  pending-delivery snapshots; sensitive-content-safe lifecycle audit; local
  Inbox/notification/export paths; and normal skill-based provider delivery.
- Phase 6 added `CommandCenterBriefAction` to the existing durable scheduler,
  frozen source and destination fingerprints, ordinary authorization snapshots,
  run-id idempotency, Inbox reports, reviewed output queues, Automations state,
  and Connections authoring.
- Phase 7 added reviewed Gmail/Google Calendar observation modes, typed
  metadata-only integration events, schema-backed fingerprints and backoff,
  deduplication, cursor expiry baselines, offline blocking, connection failure
  state, and independent cursor clearing.
- The Phase 5–7 integration gate passed 407 command-center, scheduler, policy,
  router, storage, and offscreen UI tests. Ruff, MyPy, architecture boundaries,
  and documentation consistency also passed before Phase 8 began.
- Phase 8 passed the complete 1,285-test suite, independently verified 1,215
  core regression and 70 process-isolated offscreen UI lanes, a final 31-test
  changed-domain gate, 29 learning tests, Ruff, selected strict MyPy,
  architecture and documentation checks, dependency auditing, the 100/100
  adversarial evaluation, and an isolated PyInstaller build with packaged media
  verification. Live provider accounts and physical Windows UI checks remain
  external acceptance lanes.

## Product Outcome

Local Assistant becomes a local control layer across the user's existing
systems of record. It can retrieve bounded information from configured sources,
assemble a grounded work brief, help the user turn observations into reviewed
work products, and deliver those products to exact destinations through the
existing permission and confirmation pipeline.

The command-center loop is:

```text
configured sources
-> explicit or scheduled collection
-> typed, bounded observations
-> grounded brief or work artifact
-> user review
-> exact destination action
-> result, Inbox entry, and safe audit metadata
```

The assistant does not become a mailbox mirror, a background web crawler, an
unrestricted integration broker, or a second execution engine.

## User Experience

### Command Center home

The existing Dashboard remains a primary screen and grows command-center cards
without losing its current metrics behavior:

- **My Work Brief**: run or open the latest configured briefing;
- **Needs Attention**: pending approvals, failed automations, stale sources, and
  unread Inbox reports;
- **Upcoming**: bounded task, reminder, and calendar summaries;
- **Connection Health**: enabled source/destination readiness without exposing
  credentials or private content;
- **Quick Actions**: open Assistant with a grounded command such as `brief me`,
  `draft a response`, or `review pending deliveries`.

### Connections

Add a **Connections** page with three views:

1. **Sources** — places from which the assistant may retrieve information.
2. **Destinations** — places to which reviewed work may be delivered.
3. **Accounts** — provider profiles, connection health, scopes, and test or
   reconnect actions.

Use *Sources* and *Destinations* in the UI rather than *Inputs* and *Outputs* so
the feature is not confused with microphone, keyboard, speaker, and display
configuration.

A provider may appear in both views while using one connection profile. For
example, Gmail may provide an unread-mail source and a draft-email destination.
Installing a managed extension, connecting its account, enabling its skill,
configuring a source/destination binding, and granting a runtime permission
remain distinct actions.

### Work Brief

The first end-to-end workflow is a manual **My Work Brief** containing any
enabled combination of:

- unread or query-scoped Gmail messages;
- upcoming Google or Outlook calendar events;
- selected Slack channel or search results;
- local tasks, reminders, automation approvals, and failures;
- one or more exact public HTTPS pages;
- selected Notion, GitHub, Drive, OneDrive, or approved local-file searches.

Each section shows its source, freshness, result count, partial/error state, and
addressable result items. A source failure does not erase successful sections.
Sensitive sections are not spoken unless the existing sensitive-speech setting
allows it.

### Destinations

The first destinations are local and reversible:

- Assistant response;
- durable Inbox report;
- desktop notification containing only a safe summary;
- exact new local export file.

External destinations are review-first:

- local editable email draft, then confirmed Gmail/Outlook draft creation;
- a separate confirmed send action;
- exact Slack channel or thread post;
- exact Notion page creation or bounded append;
- exact GitHub issue or comment.

Arbitrary website writes and general webhook execution are not supported.
Provider-specific reviewed skills or visible foreground automation are required.

## Design Principles

1. **One authority path.** Every external or filesystem action becomes a
   validated registered `SkillCall` and re-enters `ExecutionEngine`.
2. **Connections are not permission.** Authentication establishes identity;
   exact runtime permissions and confirmations remain separate.
3. **Source content is untrusted data.** Email, pages, messages, and documents
   cannot grant authority, select destinations, or suppress confirmation.
4. **Metadata before content.** The planner may receive configured source names,
   capabilities, and health. It receives private content only after an explicit
   source collection action.
5. **Transient by default.** External result bodies remain in memory unless the
   user explicitly saves a Work Artifact. Audit and metrics retain safe
   metadata, not content.
6. **Draft before send.** Dynamic external communication is reviewable and is
   never silently sent.
7. **Bound every operation.** Item counts, characters, timeouts, concurrency,
   polling, retries, retention, and source scopes have explicit limits.
8. **Degrade independently.** One unavailable provider produces a partial brief,
   not a failed command center.
9. **Preserve offline behavior.** The global Offline lock blocks remote source
   collection and destination delivery before network access.
10. **Keep the UI replaceable.** Qt depends on a controller/service boundary,
    while command-center domain code does not import the desktop package.

## Existing Architecture Reused

| Concern | Existing owner | Command-center use |
| --- | --- | --- |
| Account metadata and secrets | `integrations.connections`, `storage.connections`, `SecretVault` | Reference one profile from many bindings; never copy credentials into binding configuration. |
| Read and write operations | Registered skills | Definitions reference stable skill IDs and fixed operations. |
| Validation, permission, confirmation, audit | `ExecutionEngine` | All compiled calls use the normal engine. |
| Parallel bounded work | `ExecutionPlan` | Independent source calls run concurrently with plan timeouts. |
| Addressable observations | `SkillResult.items` | Brief sections retain typed result items and provenance. |
| Planner awareness | Context providers | Expose configuration/health metadata only. |
| Recurrence and background authorization | `assistant.scheduling` | Freeze source calls and preserve current background rules. |
| Local reports and alerts | Inbox and notification center | Store report references and bounded safe summaries. |
| Command-center status cards | Dashboard service and provider registry | Add UI-independent providers rather than scraping widgets. |
| Installation and account setup | Extension Hub | Deep-link existing managed extension and connection dialogs. |

## New Domain Boundary

Add `src/assistant/command_center/` with these modules:

```text
contracts.py   Strict persisted and runtime contracts.
registry.py    Reviewed source/destination contributions and duplicate checks.
storage.py     Versioned bindings, artifacts, pending deliveries, and migrations.
service.py     Validation, call compilation, collection, rendering, and delivery.
builtins.py    Contributions backed by existing built-in and managed skills.
providers.py   Dashboard/context provider facades over safe command-center state.
```

The package may import contracts, skills, scheduling contracts, policy
contracts, storage interfaces, and integration connection contracts. It must not
import Qt, router handlers, provider-specific network adapters, or model
implementations.

## Contracts

### SourceDefinition

Catalog metadata for one reviewed source:

- stable `source_id`;
- provider and display metadata;
- stable backing `skill_id` and fixed operation, when skill-backed;
- supported use modes: `on_demand`, `briefing`, `observation`;
- connection, remote, sensitivity, and background flags;
- configuration schema identifier and safe defaults;
- maximum item and character limits.

Definitions are application-owned. Do not infer read/write semantics from an
operation name or accept arbitrary user-provided skill IDs.

### SourceBinding

One user-configured source:

- UUID and definition ID;
- display name and enabled state;
- optional `ConnectionProfile` reference;
- strictly validated connector configuration;
- enabled use modes;
- item and character limits no larger than the definition maximum;
- optional minimum freshness;
- content-retention policy, defaulting to `transient`;
- created and updated timestamps.

Secrets, OAuth scopes, raw result bodies, arbitrary headers, code, templates,
and unrestricted URLs are forbidden.

### DestinationDefinition and DestinationBinding

Definitions identify a local sink or a reviewed backing write skill. Bindings
contain an exact configured target and one delivery mode:

- `local` — no external side effect;
- `draft` — create a reviewed draft only;
- `confirm_each_time` — every delivery requires a fresh confirmation;
- `approval_queue` — scheduled work creates a pending delivery for later review.

No initial binding supports unattended dynamic external delivery.

### SourceResult and WorkBrief

`SourceResult` retains:

- binding and definition identity;
- collection timestamp and state;
- safe summary and count;
- bounded typed `RequestResultItem` values;
- sensitivity and freshness;
- a safe error code, never raw provider exception content.

`WorkBrief` retains an ordered tuple of source results, an overall
completed/partial/failed state, generation time, a safe summary, and optional
rendered display text. Source content remains untrusted even after collection.

### WorkArtifact

A reviewable deliverable derived from a brief or explicit user request:

- UUID, kind (`briefing`, `message`, `report`, `task`, `note`, `file`);
- title and bounded body;
- optional structured items and source references;
- sensitivity and provenance;
- `draft`, `ready`, `delivered`, `failed`, or `cancelled` state;
- created, updated, and optional delivered timestamps.

Artifact bodies are stored only after an explicit save or when a configured
local scheduled briefing requires a durable report. Retention and clear-data
controls are mandatory.

### PendingDelivery

A pending delivery binds an immutable artifact revision to one exact destination
binding and stores:

- artifact and destination IDs;
- an action digest;
- compiled exact target and safe preview;
- risk and required capabilities;
- pending/approved/delivered/failed/cancelled state;
- creation and expiry times;
- optional resulting skill correlation ID.

Approval must compile and revalidate a fresh `SkillCall`; changed artifacts,
bindings, skills, scopes, or targets invalidate the pending request.

## Service Responsibilities

`CommandCenterService` owns:

- catalog and binding validation;
- connection/skill availability reporting;
- compilation of reviewed bindings into validated calls;
- read-only source-plan construction;
- bounded concurrent collection through `ExecutionEngine`;
- partial-result normalization and deterministic brief rendering;
- artifact creation and revision;
- local delivery;
- external delivery preparation and execution through the engine;
- safe lifecycle audit events and diagnostic counters;
- scheduler authoring helpers for frozen briefing configurations.

It does not own provider API clients, secrets, permission grants, confirmations,
model prompts, Qt widgets, or scheduler leasing.

## Connection Strategy

`ConnectionProfileStore` is the canonical profile store for new command-center
bindings. The existing managed Google account may continue to expose a shared
service without a profile UUID during migration; bindings support a provider
default when a managed `ConnectionBinding` has no profile ID.

The Dashboard's `ProviderConnection`/`DashboardConnectionStore` remains
backward compatible initially. Add an adapter that can resolve a dashboard
connection to a command-center profile reference. Migrate provider by provider
only with tests proving secret references, deletion, active-profile selection,
and rollback. Never move or delete credentials merely to display a unified UI.

## Security and Privacy Model

### Prompt injection

Every source result is labeled untrusted. The renderer or optional local
summarizer may classify or summarize it but cannot return executable calls.
Actions require a separate planner/registry/engine cycle after the user chooses
an exact result or artifact.

### Audit and telemetry

Before source aggregation ships:

- `SkillResult.sensitive=True` must cause audit payload omission;
- sensitive arguments must use allowlisted safe metadata rather than generic
  serialization;
- command-center audit events contain IDs, counts, states, capability names,
  duration, and safe error codes only;
- metrics never contain source queries, titles, send targets, report text, or
  external payloads.

### Retention

- transient source results are never stored;
- saved artifacts have configurable bounded retention;
- pending deliveries retain only the required immutable artifact reference and
  safe action metadata;
- deleting a binding does not delete its account profile without separate
  confirmation;
- deleting a profile disables dependent bindings and deletes only that
  profile's secret references;
- Privacy controls can clear artifacts, pending deliveries, and source cursors
  independently.

### External writes

External writes remain exact, previewed, confirmed, non-retryable unless the
underlying skill explicitly proves idempotency, and subject to current
background restrictions. A scheduled dynamic artifact can enter an approval
queue but cannot become a send/post call before review.

## Phase 0: Safety and Compatibility Baseline

**Implemented August 19, 2026.** Sensitive skill arguments and results now use
content-omitting audit representations, generic connection references resolve
through `ConnectionProfileStore`, and adjacent execution/integration/policy
regressions pass.

### Work

1. Add sensitive-result audit omission and regression tests covering Gmail,
   Slack, Notion, and an arbitrary sensitive fake skill.
2. Add safe-argument audit hooks for sensitive integration calls where generic
   argument redaction is insufficient.
3. Add connection-profile resolver interfaces without changing current OAuth
   or dashboard behavior.
4. Add architecture-boundary tests for the new package.
5. Record existing dirty-worktree files and avoid destructive rewrites.

### Exit criteria

- sensitive result bodies, items, display/speech text, and errors are absent
  from audit rows;
- non-sensitive audit behavior remains compatible;
- existing integration, permission, execution, extension, and dashboard tests
  pass;
- no credentials or secret references are migrated or deleted.

## Phase 1: Command-Center Domain Foundation

**Implemented August 19, 2026.** Strict definitions and bindings, reviewed
contribution registries, versioned binding persistence, availability and call
compilation services, bootstrap composition, and architecture boundaries are
covered by the command-center regression suite.

### Work

1. Add strict contracts and enum states.
2. Add registries with stable-ID and backing-skill validation.
3. Add versioned binding storage using `SettingsStore` and profile references.
4. Add service catalog, CRUD, validation, and availability APIs.
5. Compose the service in `bootstrap.Container` without changing the UI.
6. Add safe diagnostic health output.

### Tests

- unknown-field rejection and bounds;
- duplicate definition IDs;
- unknown/disabled skill references;
- invalid fixed operation or configuration;
- source/destination direction enforcement;
- connection profile existence/provider match;
- corrupted stored payload fallback without destructive overwrite;
- binding deletion does not delete a profile or secret;
- architecture imports remain valid.

### Exit criteria

- the application starts with an empty/default command-center configuration;
- catalog and binding operations are deterministic and offline;
- malformed configuration cannot reach a provider adapter.

## Phase 2: Source and Destination Catalog

### Initial source definitions

- `gmail.unread`, `gmail.search`;
- `google_calendar.upcoming`;
- `outlook_mail.unread`, `outlook_calendar.upcoming`;
- `slack.search`, `slack.channel_history`;
- `notion.search`, `notion.page`;
- `github.assigned_issues`, `github.repository_issues`;
- `google_drive.search`, `onedrive.search`;
- `web.page`;
- `local.tasks`, `local.automations`, `local.file_search`.

### Initial destination definitions

- `local.assistant`, `local.inbox`, `local.notification`, `local.export`;
- `gmail.draft`, `gmail.send_draft`;
- `outlook.draft`, `outlook.send_draft`;
- `slack.post`;
- `notion.create_page`, `notion.append`;
- `github.issue`, `github.comment`.

Definitions may land only when the backing operation already exists and its
schema, exact permissions, risk, retry behavior, and preview are covered by
tests. Unsupported catalog entries remain absent rather than appearing disabled
with invented behavior.

### Exit criteria

- catalog directions and backing operations are accurate;
- every compiled call validates through the registered skill;
- no catalog definition expands the underlying skill's authority;
- catalog tests cover at least one local, Google, Microsoft, Slack, Notion,
  GitHub, web, and filesystem contribution that is actually available.

## Phase 3: Connections User Interface

### Work

1. Add `Connections` to stable tab metadata.
2. Build a dedicated page module and editor dialogs; do not grow domain logic in
   `ui/desktop/app.py`.
3. Expose catalog, bindings, profiles, health, and CRUD through
   `DesktopController`.
4. Deep-link managed extension installation/account setup.
5. Display exact scope summaries, source modes, destination delivery behavior,
   freshness, last result state, and permission readiness.
6. Add accessible labels, keyboard navigation, empty/error/offline states, and
   safe configuration validation messages.

### Exit criteria

- the page remains responsive with slow fake health providers;
- adding/editing/cancelling bindings is lossless;
- restart restores bindings;
- no credential value is rendered or logged;
- current Dashboard, Assistant, Extension Hub, Skills, Automations, and compact
  companion UI tests continue to pass.

## Phase 4: Manual Work Brief

### Work

1. Add `collect_sources` and `build_brief` service APIs.
2. Compile selected bindings into independent `PlanStep` calls using one user
   permission context and bounded total timeout.
3. Execute only low-risk read operations; reject mutable operations even if a
   definition is misconfigured.
4. Normalize results into `SourceResult` without scraping display prose when
   typed items exist.
5. Add deterministic rendering with source headings, freshness, item titles,
   safe descriptions, and partial failures.
6. Add a `brief me` route/planner workflow and a Dashboard action.
7. Add local Assistant and Inbox delivery.

### Default MVP

Seed no private source automatically. Offer an explicit guided preset for:

- unread Gmail;
- upcoming Google Calendar;
- local tasks and automation attention;
- optional exact public HTTPS page.

### Exit criteria

- no source executes before normal permission checks;
- independent sources run concurrently and respect timeouts;
- one failure produces a partial brief;
- results include provenance and timestamps;
- source text cannot create or execute a destination call;
- transient results are absent from SQLite, audit payloads, and metrics;
- manual acceptance completes with fake offline providers.

## Phase 5: Work Artifacts and Reviewed Destinations

### Work

1. Add durable artifact rows, migrations, retention, and clear-data APIs.
2. Add artifact editor/review UI and immutable revision digests.
3. Implement local Inbox, notification, and exact non-overwriting export.
4. Compile reviewed external destinations into backing skill calls.
5. Add pending delivery records and review UI.
6. Revalidate artifact revision, binding, skill, schema, permissions, risk, and
   exact target immediately before execution.
7. Record safe lifecycle audit events and resulting correlation IDs.

### Exit criteria

- saving is explicit and retention-bounded;
- editing invalidates an older pending delivery;
- draft and send are distinct operations;
- every external write shows exact target and preview;
- cancellation and confirmation expiry are safe;
- unsafe writes never retry after unknown outcome;
- source provenance is visible but never interpreted as authority.

## Phase 6: Scheduled Briefings and Pending Delivery

### Work

1. Add a schedule-authoring helper that freezes binding revisions and compiled
   read calls.
2. Reuse recurrence, run policy, leases, background authorization, and
   permission drift checks.
3. Render a deterministic local report from structured results.
4. Store the report as a bounded artifact and publish a safe Inbox summary.
5. Convert configured external delivery into a pending approval, never an
   unattended dynamic write.
6. Surface last run, next run, partial state, source drift, and pending delivery
   on Dashboard/Automations.

### Exit criteria

- editing a source binding moves affected schedules to review;
- a revoked permission blocks the next run;
- offline mode blocks remote sources before adapters;
- crash recovery never duplicates a report or external write;
- scheduled external output stops at pending approval;
- retention cleanup does not break schedule history.

## Phase 7: Typed Integration Observations

### Work

1. Add reviewed observation definitions rather than arbitrary webhook payloads.
2. Add typed, metadata-only observations for supported provider events.
3. Start with calendar and Gmail polling because their managed adapters and
   bounded read operations already exist.
4. Persist provider cursor/fingerprint metadata only.
5. Materialize occurrences through `ScheduleStore.observe` and frozen actions.
6. Add provider minimum intervals, backoff, deduplication, cursor expiry,
   offline behavior, and connection reauthorization states.

### Observation payload limits

Observations may include provider ID, binding ID, event kind, opaque resource
ID, timestamp, safe category, and content fingerprint. They may not include
message bodies, page bodies, arbitrary headers, attachment contents, prompt
instructions, or destination calls.

### Exit criteria

- exact observation permission and source binding are both required;
- duplicate provider events create one occurrence;
- cursor resets do not replay unsafe work;
- provider payload text is never stored as a schedule trigger;
- removing or disabling a binding stops polling and clears its cursor.

## Phase 8: Hardening and Release Verification

### Required tests

- complete main regression suite;
- learning tests;
- Ruff format and lint;
- documentation consistency;
- PyInstaller module collection/build smoke test;
- UI accessibility and high-DPI checks;
- offline and permission matrices;
- adversarial prompt-injection corpus using email, Slack, Notion, and web text;
- redaction scan of SQLite, logs, audit rows, metrics, notifications, and error
  surfaces;
- rate-limit, timeout, cancellation, restart, crash, and partial-provider tests;
- representative performance test with 20 configured sources and 10
  destinations.

### Release acceptance workflows

1. Configure Gmail and Calendar as sources, run a manual partial-success brief,
   and confirm provenance/freshness.
2. Save the brief, edit a message artifact, create a Gmail draft, and separately
   confirm its send.
3. Schedule a local morning brief, restart the application, and receive one
   durable Inbox report.
4. Configure an external destination for the schedule and verify that the run
   creates a pending delivery rather than sending.
5. Revoke a connection or permission and verify the next read/write stops before
   network access.
6. Put malicious instructions in a source item and verify no destination call
   is constructed or executed.
7. Enable Offline lock and verify all remote cards, briefs, schedules, and
   deliveries stop while local sources continue.
8. Clear command-center artifacts and cursors from Privacy without deleting
   account credentials or unrelated assistant history.

## Non-Goals for the Initial Release

- mirroring full mailboxes, workspaces, drives, or websites;
- a universal full-text index over connected accounts;
- arbitrary REST, GraphQL, SQL, webhook, or script configuration;
- unattended model-generated external messages;
- browser-form automation as a generic destination;
- automatic attachment ingestion or upload;
- public inbound webhook hosting;
- team or cloud synchronization of command-center state;
- changing current permission or confirmation semantics;
- replacing Extension Hub, Skills, Automations, Inbox, Dashboard, or Audit Log.

## Implementation Discipline

For every phase:

1. Add or update this plan's status and implementation notes.
2. Add contracts and tests before broad composition wiring.
3. Run focused tests during iteration.
4. Run the phase's adjacent regression suites before proceeding.
5. Stop and correct any compatibility regression; do not defer it to the final
   phase.
6. Preserve user worktree changes and avoid destructive Git operations.
7. Update architecture, permissions, threat model, feature, and user-guide
   documentation whenever a capability becomes user-visible.

## Definition of Done

The command center is complete for its first release when:

- users can configure reviewed sources and destinations over shared connection
  profiles;
- a manual and scheduled Work Brief can collect several independent sources and
  produce a grounded partial-success report;
- source content remains bounded, transient by default, provenance-bearing, and
  unable to execute actions;
- users can save, edit, review, and deliver work artifacts to exact local or
  external destinations;
- dynamic external delivery always requires review;
- permissions, confirmations, offline mode, audit, cancellation, retries,
  background authorization, and extension enablement retain their current
  authority;
- sensitive source and artifact content is absent from audit and metrics;
- connection, source, destination, artifact, pending-delivery, and cursor data
  can be independently inspected and cleared;
- the full regression, UI, documentation, security, and packaging gates pass.
