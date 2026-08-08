# Capability Acceptance and Threat Review

Updated August 6, 2026.

This is the shared exit-gate document for the document, visual, productivity
write, and event-automation slices. Every capability uses registered schemas,
normal permission/confirmation policy, cancellation, structured results, audit
redaction, and offline behavior.

## Local documents

Accepted scope: up to five explicit files for one-shot extraction and one
explicit folder for persistent indexing. Supported formats are bounded text,
CSV, PDF, DOCX, XLSX, and PPTX. The extractor caps input bytes, characters,
files, and sections and emits line, row, page, paragraph, sheet, or slide
locators when the format permits.

Threats and controls:

- Private-folder ingestion: separate `filesystem.content.read` and
  `filesystem.content.index` grants name exact targets; there is no implicit
  home-folder crawl.
- Parser abuse: bounded ZIP/XML/PDF input, no macros or external links, no
  executable office automation, and explicit encrypted/corrupt/unsupported
  errors.
- Prompt injection: extracted text is untrusted reference content and cannot
  add skills or authority.
- Retention: one-shot text is not indexed; persistent indexes have explicit
  status/clear operations and detect file changes/deletions.
- Disclosure: results are sensitive, paths become opaque document references
  in citations, and hidden sensitive displays do not emit citations.

Automated acceptance covers every format, limits/errors, read/index scope,
modification/deletion/restart, citation linkage, cancellation, and policy
denial. Visual quality of unusual real-world documents remains sample-specific.

## Visual understanding

Accepted scope: one explicit local image for OCR, accessible description, chart
explanation, or error-dialog explanation. The skill caps encoded bytes, decoded
pixels, OCR characters, and regions. It can use local Tesseract and an optional
local Ollama vision model.

Threats and controls:

- Covert surveillance: no screenshot capture, live screen stream, clipboard,
  or camera API exists in the skill.
- Input automation: the output is guidance only and cannot click or type.
- Image bombs/parser bugs: size, dimensions, format, timeout, and output bounds
  apply before/after decode.
- OCR injection: OCR is labeled as untrusted data in any model prompt.
- Retention/cloud drift: images are not retained by the skill; no cloud vision
  fallback is selected silently.

Automated acceptance covers explicit path scope, bounds, unsupported/corrupt
images, OCR truncation, provider prompts, cancellation, sensitive results, and
absence of capture/input permissions.

## Productivity writes

Accepted operations:

- Google Drive and OneDrive: one exact download to a new local path or one
  explicit bounded upload; no overwrite, bulk move, delete, or sharing.
- Outlook: create a draft, separately send one draft ID, and create/update/delete
  one calendar event.
- Slack: post one exact message to one channel/thread.
- Notion: create one page under one parent or append bounded content to one page.
- GitHub: create one issue or add one comment to one exact repository issue.

Threats and controls:

- Scope expansion: reads, integration writes, and message sends are separate;
  connector OAuth/token scopes are reviewed per provider.
- Wrong target/content: previews display exact account resource, IDs, local
  path, subject/title/body/message, and no-overwrite rule before confirmation.
- Duplicate mutation: mutation requests are non-retryable; failed/time-out
  outcomes never claim success.
- Credential leakage: OAuth and API tokens stay in the encrypted vault and are
  never arguments, results, planner context, or audit content.
- Provider redirect/bearer leak: downloads validate the signed provider origin;
  Microsoft bearer tokens are not forwarded to signed download URLs.
- Bulk/consequential operations: intentionally excluded and rejected by schema.

Fake-provider tests cover methods, paths, bodies, scopes, previews, risk,
non-retryability, exact download/upload behavior, no-overwrite, limits,
cancellation, and redaction. Live account acceptance requires separately
configured provider test tenants.

## Event-driven automations

Accepted triggers are exact file creation/modification/deletion, process
start/stop, connected-device change/connect/disconnect, public-HTTPS text
change, and connector-supplied calendar observations. A trigger freezes the
ordinary schedule action and adds a separate `automation.observe.*` scope.

Threats and controls:

- Payload-as-prompt: observations are discriminated typed records; raw file,
  process, device, page, or calendar content is never reinterpreted at fire time.
- Monitoring expansion: file containment and policy are rechecked, process and
  device identities are exact, and web targets are public HTTPS with
  credentials/query/fragment removed before storage.
- Sensitive retention: files use metadata, processes use PID/name, devices use
  a bounded fingerprint/status, and web pages use a SHA-256 content fingerprint.
- Flood/replay: observation IDs deduplicate, trigger minimum intervals debounce,
  and existing run/rate/concurrency/failure budgets apply.
- Authority drift: schedule revision, action/policy digest, permissions, exact
  targets, risk, machine identity, and skill schema are rechecked before action.

Automated acceptance covers match boundaries, sanitization, deduplication,
debounce, policy change, source polling, payload non-interpretation, and normal
scheduler authorization. Calendar polling itself requires a configured account
source; the typed submission and execution path is provider-independent.

## Creative and lifestyle decision

Image generation/editing, travel/maps, shopping, financial summaries, coaching,
and proactive reviews remain deferred. They require separate current-source,
privacy, spend, medical/financial, and consequential-action reviews and are not
needed to close the current local-assistant release candidate.
