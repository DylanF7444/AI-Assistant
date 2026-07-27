# Dashboard Panel Implementation Plan

Updated July 25, 2026.

## Status

The initial dashboard release was completed July 25, 2026:

- Dashboard and Assistant are persisted primary full-window destinations.
- Tray actions and keyboard shortcuts can open either primary screen.
- Typed dashboard contracts, provider registry, layout storage, refresh service,
  and privacy-bounded metric aggregation are implemented.
- The read-only dashboard ships with Assistant status, request outcomes,
  execution latency, Automations, and Recent activity cards.
- Customization supports searchable add, remove/disable, ordering, size, title,
  compatible presentation, bounded refresh intervals, cancel-safe staging, and
  reset.
- Permission-gated CPU, memory, storage, and battery metrics are available.
- The reusable external connection framework and read-only Home Assistant
  provider enforce exact scopes, encrypted secrets, timeouts, offline behavior,
  and safe errors.
- Opt-in finite-numeric trends use bounded retention, clear-data controls, and
  compact native sparklines.
- Per-card scheduling, deduplication, local/external concurrency limits,
  last-good fallback, diagnostics, and cancellable shutdown are implemented.
- The production PyInstaller spec includes every dashboard domain and desktop
  module and produces the Windows executable successfully.
- Domain, migration, security, provider, navigation, worker, and Qt rendering
  regressions are covered by the main test suite.

This plan adds a customizable dashboard as a first-class desktop destination
while preserving the compact companion, the existing Assistant workflow, and
the current permission-gated architecture.

## Product Goal

When the full assistant window is opened from the tray, the user should be able
to choose either of two primary screens:

1. **Dashboard** for an at-a-glance view of selected local and external metrics.
2. **Assistant** for text, voice, responses, approvals, and activity.

The selected primary screen is remembered. Existing management pages such as
Automations, Skills, Permissions, Settings, and Diagnostics remain available as
secondary destinations.

The dashboard must:

- ship with useful local cards without requiring account setup;
- let the user add, remove, order, resize, and configure cards;
- support typed read-only providers for external applications;
- show where each value came from and when it was last refreshed;
- degrade per card so one slow or unavailable source does not block the page;
- honor the offline lock, permissions, target scopes, secret storage, and audit
  behavior already used by the application;
- avoid storing private external payloads as telemetry.

## Current Baseline

The existing implementation provides useful foundations:

- `assistant.ui.desktop.app` creates a compact companion and a full 16-tab
  interface inside a `QStackedWidget`.
- `DesktopWindowState` persists compact/full mode, pinning, and separate window
  geometries, but not a preferred full-window screen.
- Closing hides the window to the system tray. The tray can restore the
  companion or open the full assistant.
- `MetricsStore` persists privacy-bounded pipeline rows containing timestamp,
  correlation ID, stage, duration, numeric value, and outcome. It currently
  records routing, execution, STT confidence, and background-schedule metrics.
- The Diagnostics page reads the most recent 100 metric rows once when the page
  is constructed. It has no reusable aggregation or live refresh service.
- `SchedulerService.health()`, `ScheduleStore`, the local productivity rows,
  and the Windows device adapter can supply useful local status.
- `HomeAssistantAdapter` already supports read-only entity-state requests, and
  MQTT and paired-device integrations expose health operations.
- `SettingsStore` can persist versioned JSON configuration, while
  `SecretVault` stores DPAPI-encrypted credentials outside the settings
  database.
- `PermissionManager`, the offline lock, target-scoped grants, and `AuditStore`
  provide the security model that external dashboard reads must preserve.

The desktop UI is already a large module. New dashboard domain, provider, and
widget code should live in separate modules instead of adding another large
page implementation directly to `assistant.ui.desktop.app`.

## Navigation and Focus Behavior

### Primary and secondary destinations

Keep the full window's left navigation, but visibly separate it into:

- **Main:** Dashboard, Assistant
- **Workflows and extensions:** Automations, Skills, Extension Hub, Macros,
  Playbooks
- **Security:** Permissions, File Access, Audit Log, Privacy
- **Knowledge:** Knowledge, Memories
- **Configuration and diagnostics:** Settings, Models, Applications,
  Diagnostics

`Dashboard` becomes the first full-window destination and `Assistant` the
second. The exact tab index must not be used as persisted state; use stable
screen IDs.

Add this field to `DesktopWindowState`:

```python
primary_screen: Literal["dashboard", "assistant"] = "assistant"
```

The default remains Assistant for backward compatibility. Selecting Dashboard
or Assistant updates and persists `primary_screen`. Visiting a secondary
management page does not overwrite it.

### Showing and focusing the window

Use explicit show behavior instead of switching pages in every Qt focus event:

| Situation | Expected behavior |
| --- | --- |
| First launch of an existing profile | Preserve compact mode and Assistant as the preferred full screen |
| First launch of a new profile | Use the current compact companion; full-window preference defaults to Assistant |
| User clicks Dashboard or Assistant | Switch immediately and persist the choice |
| Hidden/minimized window is restored in full mode | Open the persisted primary screen |
| Tray `Open Dashboard` | Enter full mode on Dashboard |
| Tray `Open Assistant` | Enter full mode on Assistant and focus the command input |
| Tray icon is double-clicked | Restore the last window mode and preferred primary screen |
| User Alt-Tabs away from Settings and back | Keep Settings visible; do not force a primary-screen switch |
| Push-to-talk or a pending approval raises the UI | Preserve active work and show the Assistant/approval surface only when interaction requires it |

Replace the single `Open Full Assistant` tray action with `Open Dashboard` and
`Open Assistant`; retain `Show Companion`, `Emergency Stop`, and `Quit`.

Add discoverable shortcuts:

- `Ctrl+1`: Dashboard
- `Ctrl+2`: Assistant
- `Ctrl+Return`: submit only from an Assistant input surface

## Dashboard Experience

### Read mode

The default dashboard should be useful immediately:

```text
+---------------------------------------------------------------------+
| Dashboard                         Last refresh 10:42:17   Refresh    |
| Local-first overview                         Customize dashboard     |
+-------------------+-------------------+-----------------------------+
| Assistant status  | Request success   | Execution latency           |
| Ready / Offline   | 98% today         | 420 ms avg / 690 ms p95     |
+-------------------+-------------------+-----------------------------+
| Automations       | Device performance                              |
| 4 active          | CPU 18%   Memory 61%                             |
| 1 needs review    | C: 72% used                                      |
+-------------------+-------------------------------------------------+
| Recent activity                                                     |
| Completed 12   Failed 1   Cancelled 0   Last request 2 min ago       |
+---------------------------------------------------------------------+
```

Every card includes:

- title and current value;
- source label;
- last-updated time;
- loading, ready, stale, permission-required, offline, empty, and error states;
- a compact details or configuration action;
- accessible names that include value and state.

The page uses a `QScrollArea` and a responsive grid:

- one column at narrow widths;
- two columns at medium widths;
- three or four columns when space permits;
- minimum card dimensions rather than hard-coded page geometry.

The page must stay usable at the current 820 by 600 full-window minimum.

### Customize mode

The first customization release supports:

- add a card from a searchable metric catalog;
- remove or disable a card;
- move a card earlier or later with keyboard-accessible controls;
- choose small, medium, or wide size;
- override its title;
- choose a compatible presentation: number, percentage, duration, status,
  progress, or compact trend;
- select a refresh interval from bounded values;
- reset to the default local layout.

Mouse drag-and-drop is a later enhancement. Deterministic move controls should
ship first because they are easier to test and remain accessible.

Configuration changes are staged in memory and saved explicitly. Cancel
restores the last saved layout.

### Default local cards

The initial layout should contain only sources that are available on a normal
installation:

1. Assistant status: ready/busy state, offline lock, and active model label.
2. Request outcomes: completed, failed, denied, cancelled, and timed-out counts
   over the selected window.
3. Execution latency: average and p95 over the selected window.
4. Automations: active count, queued runs, pending approvals, and paused state.
5. Recent activity: bounded outcome totals and last activity time.

Device CPU, memory, storage, and battery cards are offered but are not added
until the user grants `device.read`.

Do not place transcript text, response text, clipboard values, note bodies,
file names, entity attributes, or external account content in the default
layout.

## Architecture

```text
DashboardPage
    |
DesktopController
    |
DashboardService ---- DashboardLayoutStore
    |                         |
MetricProviderRegistry        +-- SettingsStore (versioned layout/config)
    |
    +-- AssistantMetricsProvider ---- MetricsStore
    +-- AutomationMetricsProvider --- SchedulerService / ScheduleStore
    +-- WindowsDeviceMetricProvider - WindowsDeviceAdapter
    +-- HomeAssistantMetricProvider - HomeAssistantAdapter
    +-- future typed providers
    |
PermissionManager / offline lock / AuditStore / SecretVault
```

Qt widgets depend on `DashboardService` through `DesktopController`. They must
not query SQLAlchemy rows, call Windows APIs, or invoke external adapters
directly.

### Proposed package layout

```text
src/assistant/dashboard/
    __init__.py
    contracts.py
    providers.py
    registry.py
    service.py
    storage.py
    builtins.py
    home_assistant.py

src/assistant/ui/desktop/
    dashboard.py
    dashboard_editor.py
```

Keep the provider package independent of PySide6 so it can be tested in the
normal offline regression suite.

## Contracts

Use strict, versioned Pydantic models. The initial contract should contain the
following concepts.

### Metric definition

```python
class MetricDefinition(StrictModel):
    metric_id: str
    provider_id: str
    display_name: str
    description: str
    value_kind: Literal["number", "percentage", "duration", "boolean", "status"]
    unit: str | None = None
    supported_presentations: tuple[str, ...]
    required_capabilities: tuple[PermissionScope, ...] = ()
    default_refresh_seconds: int = 60
    minimum_refresh_seconds: int = 15
    supports_history: bool = False
    sensitive: bool = False
```

Metric and provider IDs are stable machine-readable identifiers. Display names
are never used as persistence keys.

### Provider connection

```python
class ProviderConnection(StrictModel):
    connection_id: UUID
    provider_id: str
    display_name: str
    enabled: bool = True
    endpoint: str | None = None
    configuration: dict[str, JsonValue] = {}
    secret_names: tuple[str, ...] = ()
```

Only validated, non-secret configuration is stored in SQLite. `secret_names`
contains references to values held in `SecretVault`; it never contains the
secret values themselves.

### Dashboard card and layout

```python
class DashboardCard(StrictModel):
    card_id: UUID
    metric_id: str
    connection_id: UUID | None = None
    title: str | None = None
    presentation: str
    size: Literal["small", "medium", "wide"] = "medium"
    refresh_seconds: int = 60
    position: int
    options: dict[str, JsonValue] = {}
    enabled: bool = True


class DashboardLayout(StrictModel):
    layout_version: Literal[1] = 1
    cards: tuple[DashboardCard, ...]
```

Validate presentation compatibility, refresh bounds, unique card IDs, unique
positions, and provider-specific options before saving.

### Snapshot

```python
class MetricSnapshot(StrictModel):
    metric_id: str
    connection_id: UUID | None = None
    collected_at: datetime
    state: Literal[
        "ready",
        "stale",
        "permission_required",
        "offline",
        "empty",
        "error",
    ]
    value: float | int | bool | str | None = None
    unit: str | None = None
    summary: str | None = None
    trend: tuple[MetricPoint, ...] = ()
    error_code: str | None = None
```

Errors returned to the UI are normalized and safe. Provider exception strings
must not expose tokens, headers, response bodies, or machine paths.

### Provider protocol

```python
class DashboardMetricProvider(Protocol):
    provider_id: str

    def definitions(self, connection: ProviderConnection | None) -> tuple[MetricDefinition, ...]:
        ...

    async def collect(
        self,
        request: MetricRequest,
        connection: ProviderConnection | None,
    ) -> MetricSnapshot:
        ...

    async def health(self, connection: ProviderConnection | None) -> ProviderHealth:
        ...
```

Providers receive a validated request, not arbitrary Python expressions,
attribute paths, SQL, URLs, or template code.

## Service and Refresh Model

`DashboardService` owns:

- layout and connection validation;
- provider lookup;
- permission and offline checks;
- bounded concurrent refresh;
- per-provider timeout handling;
- in-memory last-known snapshots;
- deduplication when multiple cards request the same metric;
- audit events for configuration and permission decisions;
- safe aggregation of built-in assistant metrics.

Refresh rules:

1. Refresh when the Dashboard becomes visible.
2. Use one page timer that asks the service which cards are due. Do not create
   one unbounded timer or thread per card.
3. Do not begin a second request for the same card while one is in flight.
4. Cap total concurrent provider calls; start with four local and two external.
5. Default to 15 seconds for rapidly changing local device metrics, 60 seconds
   for application state, and five minutes for slow external summaries.
6. Suspend periodic external polling while the Dashboard is hidden.
7. Preserve the last successful value as stale after a temporary error.
8. Cancel or ignore late results after the page or application shuts down.
9. A manual refresh bypasses the due time but not permission, offline, timeout,
   or concurrency checks.

The UI receives immutable snapshots through worker completion signals and
updates only the affected cards. A slow source must not blank or freeze other
cards.

## Data Sources

### Built-in assistant metrics

Extend `MetricsStore` with bounded query and aggregation methods rather than
loading arbitrary history in the UI:

- `query(since, until, stages, limit)`;
- `outcome_counts(since, stage)`;
- `duration_summary(since, stage)` returning count, average, p50, and p95;
- `latest(stage)`.

Existing metric rows are sufficient for the first assistant cards. Add a
composite index on `(stage, timestamp)` only after verifying the query plan and
migration coverage.

Do not add transcript, prompt, response, skill arguments, external payload, or
card configuration fields to `pipeline_metrics`.

### Automations

Use `SchedulerService.health()` and bounded `ScheduleStore` queries for:

- active schedules;
- queued runs;
- pending approvals;
- paused/enabled state;
- recent completion/failure counts.

The dashboard is read-only. Actions such as pause, resume, run now, approve, or
delete remain in the Automations page.

### Local Windows device metrics

Wrap the existing async Windows device methods:

- `performance_status()` for CPU and memory;
- `storage_status()` for drive usage;
- `battery_status()` for battery and power state.

These cards require a target-scoped `device.read` grant. Failures should produce
an unavailable card, not fail desktop startup.

### External applications

External providers are read-only data sources, not arbitrary skill execution.
Each connection declares:

- exact provider type and stable connection ID;
- exact origin or local application target;
- supported metric definitions;
- required capabilities and targets;
- credential references;
- timeout and minimum polling interval;
- whether history is allowed.

Add an `integration.read` capability so external data access can be granted
separately from generic network use. A remote provider generally requires both
`integration.read` and `network`, scoped to its exact HTTPS origin.

Extend `PermissionManager` with a reusable scope-authorization entry point for
non-executing principals. Store a stable subject such as
`dashboard.home_assistant.<connection_id>` in the existing permission subject
field. Do not bypass or duplicate the permission query logic in the dashboard
service.

The offline lock rejects remote collection before any socket is opened.
External cards display `Offline` while retaining a stale last-known value in
memory.

### First external provider: Home Assistant

Home Assistant is the recommended first adapter because a validated read API
already exists.

The connection flow should:

1. Ask for a display name and HTTPS base URL. Plain HTTP remains limited to
   localhost, matching the existing adapter.
2. Store the long-lived token in `SecretVault`.
3. Test `/api/` health with a bounded timeout.
4. Request reviewed `integration.read` and `network` scopes for the exact
   origin.
5. Let the user select an exact entity ID and one supported state or numeric
   attribute.
6. Persist only entity ID, attribute name, display options, and secret
   reference.

Do not expose `call_service()` through the dashboard. Dashboard cards never
turn devices on or off.

The second provider should be chosen only after the Home Assistant contract has
proven that provider registration, permissions, offline behavior, secrets, and
error handling are reusable.

## Persistence and Privacy

Use `SettingsStore` for:

- `dashboard.layout.v1`;
- `dashboard.connections.v1`;
- the preferred full-window primary screen through
  `desktop_window_state`.

Use `SecretVault` for connection credentials with names derived from validated
connection UUIDs. Deleting a connection deletes its referenced secrets after
confirmation.

The first release keeps external snapshots in memory. This avoids silently
building a history of private third-party data. Built-in assistant trends use
the existing privacy-bounded `pipeline_metrics` table.

If persistent external trends are added later, introduce a dedicated numeric
sample table with:

- connection ID, metric ID, timestamp, numeric value, and state only;
- a short configurable retention period;
- opt-in history per connection;
- no text payloads or provider response bodies;
- schema-version migration and clear-data controls.

Audit only lifecycle and policy events:

- card or layout changed;
- provider connected, disabled, or removed;
- permission requested, granted, denied, or revoked;
- refresh blocked by offline or policy;
- provider health changed category.

Routine successful polling should use aggregated diagnostics rather than one
audit row per refresh.

## Implementation Phases

### Phase 0: Stabilize navigation assumptions — complete

Files:

- `src/assistant/ui/desktop/state.py`
- `src/assistant/ui/desktop/tabs.py`
- `src/assistant/ui/desktop/app.py`
- `tests/ui/test_desktop_state.py`
- `tests/ui/test_desktop_components.py`

Work:

- add the stable `primary_screen` state and backward-compatible validation;
- add Dashboard to tab metadata and make Dashboard/Assistant the visible Main
  group;
- add `show_primary_screen(screen_id)` without hard-coded tab indexes;
- add tray actions and shortcuts;
- preserve compact/full mode, geometry, active pipeline, and hide-to-tray
  behavior;
- add a temporary empty Dashboard page so navigation behavior can land before
  metric collection.

Exit criteria:

- hidden/minimized full windows reopen to the selected primary screen;
- secondary-page navigation does not overwrite the preferred primary screen;
- compact mode and current Assistant regression tests remain green.

### Phase 1: Dashboard domain foundation and local MVP — complete

Files:

- new `src/assistant/dashboard/contracts.py`
- new `src/assistant/dashboard/registry.py`
- new `src/assistant/dashboard/service.py`
- new `src/assistant/dashboard/storage.py`
- new `src/assistant/dashboard/builtins.py`
- `src/assistant/storage/database.py`
- `src/assistant/bootstrap.py`
- `src/assistant/ui/desktop/controller.py`

Work:

- implement strict contracts, layout persistence, provider registry, and
  service;
- add bounded `MetricsStore` aggregation APIs;
- implement assistant and automation providers;
- compose the service in `build_container`;
- expose async catalog, snapshot, and refresh operations through
  `DesktopController`;
- seed a versioned default layout only when no layout exists.

Exit criteria:

- local provider tests are deterministic and offline;
- malformed layouts fail validation and fall back to a safe default without
  deleting the stored payload;
- aggregation never returns private command content;
- a slow fake provider does not block another card.

### Phase 2: Dashboard UI and customization — complete

Files:

- new `src/assistant/ui/desktop/dashboard.py`
- new `src/assistant/ui/desktop/dashboard_editor.py`
- small integration changes in `src/assistant/ui/desktop/app.py`
- new `tests/ui/test_dashboard_page.py`

Work:

- build the responsive card grid and state rendering;
- refresh on visibility and on bounded timer intervals;
- add manual refresh and last-refresh status;
- add card catalog, add/remove, size, presentation, title, and ordering controls;
- support Save, Cancel, and Reset to defaults;
- add keyboard navigation and screen-reader labels;
- reuse existing desktop colors and card styles without coupling domain objects
  to Qt.

Exit criteria:

- the Dashboard page renders at the minimum full-window size;
- cards update independently;
- edit cancellation is lossless;
- restart restores the saved layout and preferred primary screen;
- no Qt event-loop blocking occurs during data collection.

### Phase 3: Device metrics and permission flow — complete

Files:

- add a Windows provider under `src/assistant/dashboard/`
- `src/assistant/policy/capabilities.py`
- `src/assistant/policy/permissions.py`
- permission UI labeling in `src/assistant/ui/desktop/app.py`

Work:

- add CPU, memory, storage, and battery definitions;
- add the reusable non-executing scope authorization method;
- present an in-context grant flow when a protected card is added or refreshed;
- render permission-required and denied states;
- verify once, session, always, deny, revoke, and exact-target behavior.

Exit criteria:

- no Windows status API is called before authorization;
- denial affects only the protected card;
- revocation takes effect on the next refresh;
- device-card tests use fake adapters and stay platform independent.

### Phase 4: External provider foundation and Home Assistant — complete

Files:

- new `src/assistant/dashboard/home_assistant.py`
- connection contracts and storage
- connection editor UI
- `src/assistant/bootstrap.py`
- external-provider unit and UI tests

Work:

- add `integration.read`;
- implement connection creation, health test, disable, and removal;
- store tokens only in `SecretVault`;
- add typed Home Assistant entity metrics;
- enforce exact origin/entity scopes, timeout, offline lock, refresh minimum,
  and redacted failures;
- show source and stale timestamps on every external card.

Exit criteria:

- no remote call occurs while offline lock is enabled;
- secrets never appear in settings, database rows, logs, audit details, metric
  rows, snapshots, or exceptions shown by the UI;
- an unavailable server does not freeze the dashboard;
- removing a connection disables/removes its cards and deletes its secret after
  confirmation.

### Phase 5: Hardening and optional trends — complete

Work:

- profile aggregation and repaint cost with a representative card count;
- add custom-painted compact sparklines if needed, avoiding a new charting
  dependency for the MVP;
- add opt-in numeric external history only if product requirements justify it;
- add diagnostic counters for refresh latency, timeout, stale, and error rates;
- validate PyInstaller packaging and Windows high-DPI behavior;
- update user, feature, permission, privacy, and architecture documentation.

Exit criteria:

- 24 visible cards remain responsive under mixed success, timeout, stale, and
  permission states;
- refresh work stops cleanly during shutdown;
- packaged builds include all required dashboard modules;
- accessibility and high-DPI manual acceptance passes.

## Test Plan

### Domain and storage tests

- strict contract validation and unknown-field rejection;
- unique IDs and positions;
- presentation/value-kind compatibility;
- refresh-interval bounds;
- default-layout seeding and version handling;
- corrupted stored-layout fallback without destructive overwrite;
- connection deletion and secret-reference cleanup;
- metric aggregation at empty, one-row, and bounded high-volume inputs.

### Provider and service tests

- provider registration and duplicate-ID rejection;
- deduplicated collection for identical cards;
- independent success/failure;
- timeout and cancellation;
- last-good snapshot becoming stale;
- visible/hidden polling behavior;
- manual refresh behavior;
- permission required, denial, revoke, and exact target;
- offline rejection before provider invocation;
- redaction of credential and response details.

### UI tests

- Dashboard and Assistant are the two persisted primary destinations;
- restore behavior from hidden, minimized, compact, and full modes;
- secondary page does not replace primary preference;
- responsive column counts;
- loading, ready, stale, permission, offline, empty, and error cards;
- add, remove, reorder, resize, save, cancel, and reset;
- keyboard shortcuts and focus order;
- no refresh result updates a destroyed widget;
- existing Assistant, voice, tray, approval, and management-tab tests continue
  to pass.

### Security tests

- external providers cannot declare unknown capabilities silently;
- remote HTTP is rejected except approved localhost cases;
- arbitrary URLs, expressions, SQL, templates, and attribute traversal are
  rejected by provider schemas;
- secrets are absent from SQLite, logs, audit data, metrics, and UI errors;
- external text values are not persisted as trend samples;
- dashboard reads cannot call mutating Home Assistant services or the execution
  engine.

## Acceptance Workflows

### Choose the main screen

1. Open the full assistant and select Dashboard.
2. Hide it to the tray.
3. Restore the full window.
4. Dashboard opens with the same layout.
5. Select Assistant, hide, and restore.
6. Assistant opens and command input can receive focus.

### Customize local metrics

1. Enter Customize mode.
2. Add execution p95 and automation pending-approval cards.
3. Make execution p95 wide and move it first.
4. Save and restart.
5. Layout, sizes, and ordering are preserved.

### Permission-gated device card

1. Add a CPU usage card.
2. The card requests `device.read` before collection.
3. Deny it; only that card shows permission required.
4. Grant a session scope; the card refreshes.
5. Revoke the grant; the next refresh stops before the adapter call.

### External metric while offline

1. Configure a Home Assistant connection and store its token.
2. Add one exact entity-state card and grant its reviewed scopes.
3. Enable the global offline lock.
4. The card shows Offline without making a network request.
5. Disable the lock and refresh.
6. The card shows a value, source, and timestamp.

### Partial provider failure

1. Display several local cards and two external cards.
2. Make one external endpoint time out.
3. Other cards continue to update.
4. The failed card retains its last value as stale with a safe error summary.

## Non-Goals for the First Release

- Executing actions from dashboard cards.
- Arbitrary user-authored API calls, SQL, expressions, or scripts.
- A web-based dashboard or remote sharing.
- Cloud synchronization of layouts or samples.
- Persisting external text payload history.
- A marketplace contract for third-party dashboard provider code.
- Drag-and-drop as the only layout mechanism.
- Alerts, notification rules, or background polling while the dashboard is
  hidden.
- Replacing the existing Diagnostics, Automations, or Permissions pages.

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| More code added to the large desktop module | Put page/editor widgets and all domain logic in separate modules |
| UI freezes during local or remote collection | Async controller boundary, bounded concurrency, timeouts, no duplicate in-flight request |
| Polling causes load or rate-limit problems | Provider minimums, hidden-page suspension, deduplication, manual refresh bounds |
| External data leaks into telemetry | Separate snapshots from `pipeline_metrics`, memory-only external cache, numeric opt-in history later |
| Credentials leak through errors | DPAPI vault, redacted exception mapping, adversarial tests |
| Dashboard bypasses skill permissions | Reuse `PermissionManager` scope logic with stable dashboard principals |
| Navigation changes regress the compact companion | Preserve compact/full state as a separate concern and land navigation tests first |
| Provider-specific configuration becomes arbitrary code | Typed definitions and connector-specific validated options only |

## Recommended First Deliverable

The first implementation pull request should include only Phases 0 and 1 plus a
read-only Dashboard UI shell:

- persisted Dashboard/Assistant primary selection;
- tray actions for both primary screens;
- extracted dashboard contracts and service;
- default Assistant status, request outcomes, execution latency, Automations,
  and Recent activity cards;
- manual refresh;
- local-only tests and no external connection UI.

That slice proves navigation, persistence, aggregation, async refresh, and card
rendering before introducing customization, device permissions, secrets, or
network providers.

## Definition of Done

The dashboard is complete for its initial release when:

- Dashboard and Assistant are clearly presented as the two primary full-window
  screens;
- the preferred primary screen survives hide, restore, and restart;
- users can build and persist a layout from the supported metric catalog;
- built-in cards update without blocking the Qt event loop;
- protected local and external reads are default-deny and target scoped;
- external credentials remain DPAPI-encrypted and absent from telemetry;
- offline, error, stale, and partial-failure behavior is explicit;
- existing assistant, voice, scheduling, permission, and tray regressions pass;
- the packaged Windows build passes the acceptance workflows above.
