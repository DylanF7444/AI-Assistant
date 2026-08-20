# Architecture

## Data flow

1. A push-to-talk source emits mono 16 kHz PCM and pipeline events.
2. An interchangeable offline STT provider emits immutable raw and normalized
   transcripts.
3. Deterministic routing handles common commands. For an unmatched request,
   semantic candidate selection gates demand-driven context collection and one
   schema-constrained local-model completion jointly interprets and answers or
   plans the request.
4. The selected registered skill validates its Pydantic input schema.
5. Policy checks user, skill, capability, session, and target scopes.
6. Sensitive calls produce an immutable, expiring confirmation instead of
   executing.
7. The execution engine revalidates and reauthorizes immediately before work.
8. Structured results produce display and optional speech responses.
9. Permission decisions and outcomes are written to a separate audit log.

## Command center

The command center is a composition layer, not a second execution engine.
`CommandCenterRegistry` owns reviewed source/destination definitions;
`CommandCenterBindingStore` persists non-secret user bindings that reference the
shared connection-profile store. Source reads compile to ordinary `SkillCall`
objects and pass through the existing registry, permission manager, execution
engine, cancellation, offline lock, and audit controls.

The desktop catalog is availability-aware: new binding editors include a
skill-backed definition only when that skill is built in or installed. Existing
bindings are never hidden after uninstall; they become unavailable and retain
an exact Extension Hub repair link. Extension Hub owns package and account
management, while Connections owns bindings and reviewed work. The shared Work
Brief authoring dialog creates a durable schedule and hands its exact ID to the
Automations view.

Manual Work Briefs collect independent low-risk source calls concurrently and
normalize typed `SkillResult.items` into bounded provenance-bearing sections.
They are transient unless explicitly saved. Saved `WorkArtifact` rows are
retention-bounded and revisioned with immutable digests. A `PendingDelivery`
freezes the artifact revision, destination binding digest, and exact target;
all three are revalidated immediately before normal destination execution.

Scheduled briefs use `CommandCenterBriefAction` inside the durable scheduler.
The action freezes each source call and binding digest plus the ordinary skill
version/schema/permission/risk snapshots. One schedule run creates at most one
artifact by durable run ID, posts an idempotent Inbox report, and queues any
configured output for review rather than executing a dynamic write.

Reviewed Gmail and Google Calendar observation bindings use typed integration
events. The poller runs only through an active schedule's frozen source call and
stores a SHA-256 fingerprint, provider/binding IDs, poll times, and backoff
state. Message/event text, headers, bodies, attachments, and destination calls
are never cursor data. A first or expired cursor establishes a baseline without
replaying an event.

## Request results and selection

`SkillResult.items` is the shared result-item envelope for any skill that
returns an addressable collection. Each `RequestResultItem` has an ID, kind,
display title and description, a schema-friendly value for later work,
metadata, and an optional sensitivity marker. `None` means the request did not
produce an item collection; an empty tuple is an intentional empty result set.
The response layer carries the typed items separately from display text, so the
assistant and UI never need to scrape numbered prose.

Conversation state retains the latest item set, its producing skill and the
current selection. Users can select an item by ordinal, item ID, or
unambiguous title. The session context provider exposes bounded
`results.recent_items` and `results.selected` records, including their concrete
values, to grounded follow-up planning. Sensitive item values are redacted.
Legacy `files.selected` and `files.recent_candidates` context remains as a
compatibility view over file-kind items.

## Media presentation

`media.present` and `media.viewer_control` use the normal router, schema, policy,
permission, engine, response, and audit flow. A successful preparation returns
an immutable `MediaDirective`; it does not claim that decoding has succeeded.
The desktop applies the directive only to its matching in-memory presentation
session, rechecks the source fingerprint, and separately records loading, ready,
playing, paused, stopped, failed, fullscreen, and closed state.

Local sources are canonical exact files or bounded filename searches beneath an
approved root. Explicit remote sources pass public-HTTPS and address validation,
download into a size/age-bounded content-addressed cache, and pass MIME and
signature validation before Qt receives a local path. Qt never receives an
unvalidated network URL. Query-free provenance is retained in memory; URL query
strings, decoded content, thumbnails, and frames are excluded from audit and
diagnostic logs.

Images decode on bounded worker threads through `QImageReader`. Audio and video
use a testable backend around `QMediaPlayer`, `QAudioOutput`, and `QVideoWidget`.
An audio-focus coordinator aggregates named TTS and viewer outputs before
suppressing microphone capture. The desktop pauses viewer media for an explicit
assistant interaction and resumes only the same unmodified session. Emergency
Stop and shutdown stop playback.

## Structured visual presentation

`core.visualize` produces an immutable `VisualArtifact` containing a bounded
chart, flowchart, timeline, or table specification plus accessible alternative
text. The artifact follows the ordinary skill, execution, response, audit, and
plan aggregation path. It is separate from `MediaDirective`, is not stored in
the media session registry, and requires no filesystem or network permission.

`assistant.visualization.parser` is shared by deterministic routing and direct
`core.visualize` natural-language calls. It converts typed text or normalized
speech into the same typed specification while treating punctuation and spoken
equivalents as optional cues. The skill also accepts a precise structured spec,
so planner-generated and conversational visuals converge after parsing.

The desktop sends the artifact to `VisualizationView` in the existing results
window. The local QPainter renderer treats all labels as text, while the
conversation, terminal, and speech paths retain the artifact's text fallback.
If Qt is unavailable, callers still receive the typed artifact and its
description.

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

The optional logic layer is an additive scheduled-action variant rather than a
second execution system. The planner maps an immediate selection to
`core.choose`, and maps delayed or recurring control flow to `core.schedule`
with a `logic_workflow` action. A logic workflow may contain one frozen check,
a typed predicate over its structured result, a random or history-driven selection, one
notification or frozen registered-skill action, and an explicit completion
policy. Delay and repetition remain ordinary scheduler triggers; `max_runs`,
expiry, condition, and success limits provide the stop behavior.

At authoring time, every possible nested skill call is schema-validated and
snapshotted. Its permissions, exact targets, background compatibility, and risk
are folded into the existing schedule authorization. At run time, the logic
evaluator can inspect only whitelisted `SkillResult` fields and non-sensitive
typed result items. Selected calls still re-enter `ExecutionEngine`; the model
does not generate code or reinterpret the workflow when it fires. SQLite stores
bounded choice history, retry-stable choice checkpoints, and run/success/stop
counters, while raw check results remain subject to the existing run-result and
audit handling.

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

The planner latency path performs retrieval first, derives a bounded set of
candidate skills, and collects only context relevant to the request or those
candidates. Independent context providers run concurrently while process and
device snapshots use short thread-safe caches. The collected context,
retrieval results, and interpretation fields share one grounded completion
schema, avoiding a separate interpretation model round trip. Simple factual
answers receive a smaller output-token ceiling than detailed explanations or
settings guidance.

The Ollama adapter sends a configurable `keep_alive` value on every completion
and can preload the selected model in a background startup request. Native
Ollama load, prompt-evaluation, generation, token, and total-duration fields
are recorded alongside planner retrieval/context/grounded stages and total
request latency. Metrics are correlated per request and contain timing and
counts rather than prompt or response text.

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

## Document and image understanding

`assistant.skills.documents` is a bounded extraction boundary for explicit TXT,
Markdown, CSV, PDF, DOCX, XLSX, and PPTX paths. It emits typed extracted
sections with opaque source references and format-specific locators. Persistent
content search stores signatures and bounded chunks in the local vector-index
boundary, reconciles modifications/deletions under a lock, and never changes
source files. Content-read and content-index retention are separate policy
capabilities.

`assistant.skills.vision` accepts one explicit local image and one of four typed
operations: OCR, accessible description, chart explanation, or error-dialog
explanation. Optional Tesseract and vision providers are injected. The skill has
no screen/camera acquisition, clipboard, input, or automatic retention path.

## Event observations

Event schedules use typed `EventTrigger` contracts and `EventObservation`
records rather than treating source payloads as prompts. `FileEventSource`,
`ProcessEventSource`, `DeviceEventSource`, and `WebEventSource` collect only
bounded matching metadata or fingerprints for one exact scoped target. Web
targets are sanitized public HTTPS locations and flow through the ordinary
offline-aware web client. Calendar observations use the same contract but enter
only through an explicitly configured connector. Matching observations create
durable scheduler occurrences, after which the frozen action uses the same
lease, authorization, permission, confirmation, and execution path as a clock
trigger.

## Evaluation and observability

`assistant.evaluation` owns versioned cases, corpus loading, production-planner
harnesses, deterministic scorers, confidence intervals, and JSON/Markdown
reports. It invokes the production planner boundary; it does not duplicate a
separate evaluation-only planner. `planner_contracts` contains the stable
validated decision/revision models, while orchestration continuation state
lives under `assistant.orchestration` to prevent planner/engine ownership leaks.

`assistant.observability` defines typed stage and dimension names for correlated
request, voice, retrieval, planner, provider, execution, and automation timing.
Only durations, counts, states, provider/model identifiers, and sanitized
categories are stored—never prompts, transcripts, responses, document text, or
credentials. Dashboard providers aggregate that store rather than scraping UI
state.

## Dashboard

`assistant.dashboard` is a UI-independent read model. Strict card, layout,
connection, definition, request, snapshot, and numeric point contracts flow
through `DashboardService` and a provider registry. Built-in providers aggregate
only bounded `MetricsStore`, local task, and scheduler data, including request,
model, skill-execution latency, and task summary cards. The Windows device provider
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

Retrieved workflow templates may declare registered `skill_ids`, alternative
`skill_stages`, and terminal `completion_skill_ids`. For these result-dependent
goals, the planner selects only the currently executable stage and binds only
that skill's arguments. The orchestrator executes the call through normal
permissions, records its typed result, then asks the planner to select and bind
the next stage. A workflow cannot skip a declared stage or report completion
before a completion skill succeeds. Observation compaction prioritizes the
newest result so an early large search response cannot hide a later fetched
document from argument binding.

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
  background authorization, the database-backed notification Inbox, delivery,
  leasing, approvals, and the background runtime. It invokes skills only
  through `ExecutionEngine`.
- `assistant.dashboard` owns metric contracts, providers, registry, validated
  layout/connection/sample storage, permission-aware refresh, history, and
  diagnostics. Qt dashboard widgets depend on it only through
  `DesktopController`.
- `assistant.evaluation` owns corpus contracts, planner harnesses, scoring, and
  report generation; production planner decisions remain owned by
  `assistant.language`.
- `assistant.skills.documents` and `assistant.skills.vision` own bounded content
  interpretation, while file policy and authorization remain external policy
  boundaries.
- `assistant.scheduling.event_sources` converts exact external state into
  privacy-bounded observations; it never invokes a skill directly.
- `assistant.presentation` owns probing, local reference resolution, guarded
  remote acquisition/cache policy, and in-memory presentation sessions.
- `assistant.ui.desktop.notification_widgets` owns transient foreground
  presentation while the scheduling notification store remains authoritative.
- `assistant.ui.desktop.media_backend`, `media_viewer`, and `result_window` own
  Qt decoding, detached presentation, playback, report display, controls, and
  applied/failed lifecycle reporting.
- `assistant.ui.desktop.visualization_view` owns local rendering of structured
  visual artifacts and remains independent from media decoding and playback.

The dependency direction remains contracts/interfaces -> policy and domain
services -> orchestration -> UI/composition. Model output never calls platform
adapters directly.
