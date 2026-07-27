# Architecture

## Data flow

1. A push-to-talk source emits mono 16 kHz PCM and pipeline events.
2. An interchangeable offline STT provider emits immutable raw and normalized
   transcripts.
3. Deterministic routing handles common commands. Only unmatched requests may
   reach a local schema-constrained planner.
4. The selected registered skill validates its Pydantic input schema.
5. Policy checks user, skill, capability, session, and target scopes.
6. Sensitive calls produce an immutable, expiring confirmation instead of
   executing.
7. The execution engine revalidates and reauthorizes immediately before work.
8. Structured results produce display and optional speech responses.
9. Permission decisions and outcomes are written to a separate audit log.

## Application self-knowledge

Open-ended questions about Local Assistant itself use the same planner path as
other conversational requests. For questions such as `how do you work`, the
planner receives a trusted, code-owned product summary from
`assistant.language.application_knowledge`. It describes the request lifecycle,
skill and policy boundaries, memory and knowledge behavior, optional-provider
availability, and the limits of model introspection. The context is injected
only for application-knowledge requests so unrelated prompts retain their
existing size.

Questions about a skill also receive a compact live directory of currently
enabled skill manifests, including description, risk, and permission
capabilities. Named skills can additionally be supplied as detailed planning
candidates with their input schema. Referential follow-ups such as `explain
this skill` use bounded recent-session context; when that context does not name
a skill, the planner is instructed to ask which skill the user means.

This product summary is descriptive context, not executable authority. It does
not add skills, permissions, or access, and it explicitly prevents claims of
subjective self-awareness, hidden-model introspection, source-code inspection,
or unseen runtime state. Deterministic commands still work without a planner,
but open-ended application explanations require the configured local planner.

## Durable scheduling and background execution

`assistant.scheduling` owns a deterministic, SQLite-backed scheduler. Schedule
definitions contain a typed trigger, frozen action or plan, explicit autonomy
bounds, delivery policy, run policy, skill snapshots, and a monotonically
increasing revision. Before any side effect, the scheduler durably creates an
occurrence, claims it with a lease, validates its action and policy digests, and
re-enters the normal registry, schema, file-policy, permission, confirmation,
execution, cancellation, audit, and metrics pipeline.

The desktop owns one long-lived background asyncio loop. SQLite occurrence keys
and compare-and-set leases prevent duplicate claims across processes; a Windows
per-user mutex prevents duplicate desktop runtimes. Lease heartbeats protect
long jobs, and startup reconciliation marks an interrupted running action as an
unknown outcome instead of replaying it. Missed runs follow stored skip,
run-once, or bounded catch-up policies using IANA time zones and explicit DST
gap/fold rules.

Low-risk fixed calls and frozen macro snapshots may run unattended only with a
matching schedule-specific background authorization. Medium, high, and critical
occurrences stop in `awaiting_approval`; reviewing them creates fresh,
occurrence-bound engine confirmations, including the existing typed token for
critical work. Background authorizations are revision-, digest-, target-,
capability-, risk-, expiry-, and machine-bound and are separate from interactive
permission grants.

Background compatibility is enforced independently of risk and permission.
Exact calls that can focus or launch windows, send input, use the clipboard,
control media, terminate processes, or visibly change the device are rejected
at schedule authoring and rechecked before every occurrence. Background pipeline
events carry a safe structured activity trace and never mutate the foreground
desktop pipeline state.

Optional headless web rendering uses a new non-persistent Playwright browser
context per call. Navigation is public-HTTPS-only, local/private targets and
downloads are blocked, mutating HTTP methods are aborted, results are bounded,
and the user's visible browser profile is never attached. Result-oriented reads
rank visible article/card/product/listing structures and return a bounded set of
matching items. Windows builds prefer an installed Edge or Chrome runtime and
can fall back to Playwright Chromium when it has been installed.

For unmatched conversational requests, enabled local knowledge bases are
searched with bounded lexical retrieval. Relevant excerpts are included as
untrusted reference data in the schema-constrained planner prompt. Knowledge
files are stored as local database content and never execute instructions.

Enabled user playbooks are loaded from validated Markdown files under the local
data directory and indexed in a separate retrieval namespace. Unlike knowledge,
they are explicit user-authored planning guidance. Their structured skill IDs
can prioritize only currently enabled registered skills; missing IDs remain
visible metadata and are never invented. Playbook prose and per-skill guidance
cannot add capabilities, grant permission, suppress confirmation, or bypass the
execution engine. Scheduled actions continue to use reviewed frozen calls and
never reinterpret a playbook at fire time.

The UI depends on the orchestrator and event bus, not model or OS
implementations. STT, LLM, TTS, wake-word, embedding, Windows, storage, clock,
and device implementations are injected behind typed interfaces.

## Dashboard

`assistant.dashboard` is a UI-independent read model. Strict card, layout,
connection, definition, request, snapshot, and numeric point contracts flow
through `DashboardService` and a provider registry. Built-in providers aggregate
only bounded `MetricsStore` and scheduler data. The Windows device provider
uses the existing device interface, and the Home Assistant provider exposes
only health and exact entity-state reads.

`DashboardService` validates layouts and provider options, deduplicates
identical card requests, applies separate four-local/two-external concurrency
limits and timeouts, and retains last-good snapshots in memory. One Qt page
timer selects due card IDs while the page is visible; collection runs in
cancellable workers, and immutable snapshots update only affected widgets.
Provider errors are normalized without returning exception content.

Device and external reads re-enter `PermissionManager` through
`authorize_scopes` using stable non-executing dashboard principals. Remote
providers are stopped before adapter invocation when the offline lock is on.
Home Assistant tokens are referenced from `SecretVault`, never connection
settings. Lifecycle and first-category block events are audited without card
titles, options, values, or response payloads; routine polling is summarized by
diagnostic counters.

External history is connection-level opt-in. `dashboard_metric_samples` stores
only timestamp, connection ID, generic metric ID, a SHA-256 series key, finite
numeric value, and state. Text values are rejected, retention is bounded to
1–90 days, and connection deletion clears samples and vault references.

The implemented context-aware routing and planning roadmap is documented in
`docs/SMART_ASSISTANT_IMPLEMENTATION_PLAN.md`. It records the design history
for semantic retrieval, context snapshots, intent interpretation, preference
memory, and adaptive replanning while preserving the permission-gated
execution model described above.

## Compatibility

Core contracts and skill manifests use API version `1.0`. Minor core releases
must preserve existing fields and meanings. A breaking contract change requires
a new major API version and side-by-side loader support or a documented
migration. Stable skill IDs are never inferred from display names.

## Module boundaries

- `assistant.router.handlers` owns ordered deterministic command families;
  `DeterministicRouter` remains the precedence coordinator and public facade.
- `assistant.orchestration` owns conversation state, file resolution, planner
  coordination, interaction recording, and voice confidence gating.
- `assistant.execution.evaluation` provides the shared typed evaluation used
  by actions, plans, and final execution reauthorization.
- `assistant.windows.interfaces` contains platform protocols. Capability
  modules contain implementations, while the former adapter and skill modules
  preserve legacy imports.
- `assistant.ui.main` lazily loads the optional desktop package, whose workers,
  controller, tab metadata, and application startup are independently testable.
- `assistant.bootstrap` is the composition root and uses named builders for
  storage, retrieval, registry, context, and audio services.
- `assistant.scheduling` owns trigger calculation, durable definitions and runs,
  background authorization, notification delivery, leasing, approvals, and the
  background runtime. It invokes skills only through `ExecutionEngine`.
- `assistant.dashboard` owns metric contracts, providers, registry, validated
  layout/connection/sample storage, permission-aware refresh, history, and
  diagnostics. Qt dashboard widgets depend on it only through
  `DesktopController`.

The dependency direction remains contracts/interfaces -> policy and domain
services -> orchestration -> UI/composition. Model output never calls platform
adapters directly.
