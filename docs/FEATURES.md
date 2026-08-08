# Feature Catalog

Updated August 6, 2026. This catalog lists the built-in features registered by
the application container. Optional integrations are implemented but require
the relevant setting, credential, local service, or device to be configured.

## Status Legend

- `Implemented`: available in the default local application.
- `Optional`: implemented but requires explicit configuration or network access.
- `Extensible`: implemented framework support for user-defined entries.

## Built-In Skills

| Area | Skill ID | Status | Risk | Permissions | Capability |
| --- | --- | --- | --- | --- | --- |
| Core | `core.calculator` | Implemented | Low | None | Evaluate arithmetic locally without executing code. |
| Core | `core.chance` | Implemented | Low | `notification.create` | Roll an immediate percentage chance and show the requested notification only when it triggers. |
| Core | `core.choose` | Implemented | Low | None | Choose from two to 100 values using random, seeded, weighted, round-robin, or least-recent selection with optional filters and bounded history. |
| Core | `core.datetime` | Implemented | Low | None | Return the local date, time, or both. |
| Core | `core.diagnostic` | Implemented | Low | None | Report local hardware, microphone, model, and skill health. |
| Core | `core.file_access_policy` | Implemented | High | `settings.get`, `settings.set` | List and change filesystem read and write/delete allow and block lists. |
| Core | `core.help` | Implemented | Low | None | Explain available commands, skills, and required permissions. |
| Core | `core.macro` | Extensible | Low | Per macro step | Run an existing configured sequence by its exact saved name without bypassing validation, permissions, or confirmations. |
| Core | `core.notes` | Implemented | Low | None | Add, search, list, and delete local notes. |
| Core | `core.playbook` | Extensible | Dynamic | None | Save, list, show/recall, enable, disable, or confirmation-gated delete of Markdown planning playbooks. |
| Core | `core.reminder` | Implemented | Low | None | Create local reminders or alarms. |
| Core | `core.schedule` | Implemented | Dynamic | None | Create recurring reminders, reviewed frozen actions/macros, and bounded conditional or rotating logic workflows; list, inspect, pause, resume, revise, delete, snooze, run now, and inspect or clear run history. |
| Core | `core.tasks` | Implemented | Low | None | Add, list, complete, and delete local tasks. |
| Core | `core.timer` | Implemented | Low | None | Run a cancellable local timer. |
| Core | `core.unit_conversion` | Implemented | Low | None | Convert common distance, weight, volume, and temperature units locally. |
| Core | `core.visualize` | Implemented | Low | None | Create local charts, flowcharts, timelines, and tables from natural typed/dictated language or typed specs, with accessible text fallbacks. |
| Documents | `documents.read` | Implemented/Optional formats | Low | `filesystem.content.read` | Extract, summarize, compare, find action items in, or answer questions from up to five explicitly selected TXT, Markdown, CSV, PDF, DOCX, XLSX, or PPTX files with source locators. |
| Documents | `documents.content_search` | Implemented/Optional formats | Medium | `filesystem.content.read`, `filesystem.content.index` | Build, inspect, search, or clear a bounded persistent content index for one approved folder, with modification and deletion reconciliation. |
| Vision | `vision.understand_image` | Implemented/Optional OCR | Low | `filesystem.content.read` | Read text, describe an image, explain a chart, or explain an error dialog from one explicitly selected local image; never captures the screen or performs input. |
| Network | `cloud.ask` | Optional | Low | `network`, `process.launch` for Codex | Ask configured OpenAI, Anthropic, or local Codex CLI providers. |
| Network | `web.fetch` | Optional | Low | `network` | Read text from a specific public HTTPS page. |
| Network | `web.headless_read` | Optional | Low | `network` | Render a public HTTPS page in a fresh non-persistent headless browser and return bounded visible text/links or matching deal, product, article, listing, and result cards without opening a user window. |
| Network | `web.news_search` | Optional | Low | `network` | Retrieve current headlines with source and publication time for reports and briefings. |
| Network | `web.search` | Optional | Low | `network` | Search the public web through DuckDuckGo or Brave Search. |
| Connections | `integration.google_calendar` | Extension Hub/Optional | Dynamic | `integration.read`, `integration.write`, `network` | Install the first-party Hub extension, connect Google with in-app encrypted OAuth, and list or manage calendars, events, availability, invitations, settings, and sharing rules. |
| Connections | `integration.gmail` | Extension Hub/Optional | Dynamic | `integration.read`, `integration.write`, `messaging`, `network` | Install the first-party Hub extension, connect Google with in-app encrypted OAuth, and search or read messages and threads, manage drafts and mailbox state, and send explicitly approved email. |
| Connections | `integration.google_drive` | Extension Hub/Optional | Dynamic | `integration.read`, `integration.write`, `filesystem.content.read`, `filesystem.write`, `network` | Search and inspect Drive; download one exact file to a new local path or upload one explicit local file without overwriting. Sharing, moves, and deletes remain excluded. |
| Connections | `integration.outlook_mail` | Extension Hub/Optional | Dynamic | `integration.read`, `integration.write`, `messaging`, `network` | Search and inspect Outlook Mail, create one explicit draft, and send one exact existing draft after confirmation. Deletion and bulk changes remain excluded. |
| Connections | `integration.outlook_calendar` | Extension Hub/Optional | Dynamic | `integration.read`, `integration.write`, `network` | Search and inspect Outlook Calendar; create, update, or delete one exact event after confirmation. Bulk changes and meeting responses remain excluded. |
| Connections | `integration.onedrive` | Extension Hub/Optional | Dynamic | `integration.read`, `integration.write`, `filesystem.content.read`, `filesystem.write`, `network` | Search and inspect OneDrive; download one exact file to a new local path or upload one explicit local file without overwriting. Moves and deletes remain excluded. |
| Connections | `integration.slack` | Extension Hub/Optional | Dynamic | `integration.read`, `messaging`, `network` | Search and read bounded Slack content and post one exact confirmed message to an exact channel or thread. Reactions, uploads, and broad ingestion remain excluded. |
| Connections | `integration.notion` | Extension Hub/Optional | Dynamic | `integration.read`, `integration.write`, `network` | Search and read shared Notion content, create one page under an exact parent, or append bounded content to one exact page. Database writes and broad ingestion remain excluded. |
| Connections | `integration.github` | Extension Hub/Optional | Dynamic | `integration.read`, `integration.write`, `network` | Search and inspect repositories/issues/releases, create one issue, or add one confirmed comment to an exact issue. Code, workflow, secret, and bulk mutations remain excluded. |
| Media | `media.present` | Implemented/Optional remote | Low | `filesystem.read` or `network` | Present an exact approved local file or explicitly downloaded public HTTPS image, audio file, or video in the Assistant. |
| Media | `media.viewer_control` | Implemented | Low | None | Control only the active in-app viewer: playback, seek, volume, rate, mute, fullscreen, stop, or close. |
| Windows | `windows.application_window` | Implemented | High | `process.window` | Focus, minimize, maximize, close, move, or resize an approved application window. |
| Windows | `windows.browser_navigate` | Implemented | Low | `process.window`, `input.control`, `network` | Open a public HTTPS site in an existing browser window. |
| Windows | `windows.clipboard` | Implemented | Medium | `clipboard.read`, `clipboard.write` | Read or write the local clipboard with separate permissions. |
| Windows | `windows.context_navigation` | Implemented | Medium | `process.window`, `input.control` | Use browser tab, page, and address-bar shortcuts in the focused or visible browser. |
| Windows | `windows.device_control` | Implemented | Critical | `device.read`, `device.control`, `media.control`, `system.apps`, `system.display`, `system.network`, `system.power` | Search installed apps, control volume/brightness/radios/power, and read local device status. |
| Windows | `windows.file_operation` | Implemented | High | `filesystem.read`, `filesystem.write` | Open, copy, move, rename, or recycle approved files, or organize one approved folder into named subfolders using explicit extension rules or standard image/text categories and an exact confirmation preview. |
| Windows | `windows.file_search` | Implemented | Low | `filesystem.read` | Search filenames inside an approved folder and return selectable numbered results. |
| Windows | `windows.input_automation` | Implemented | High | `input.control` | Type text or click visibly inside one approved active application. |
| Windows | `windows.launcher` | Implemented | Medium | `process.launch` or `network` | Launch registered application paths, resolve relational browser requests through approved preference memory, or open approved HTTPS fallback URLs. |
| Windows | `windows.media` | Implemented | Low | `media.control` | Send local media play, pause, next, previous, stop, and volume commands. |
| Windows | `windows.notification` | Implemented | Low | `notification.create` | Show a visible local desktop notification. |
| Windows | `windows.process` | Implemented | Critical | `process.terminate` | Inspect running processes or terminate an exact PID after approval. |
| Windows | `windows.registered_applications` | Implemented | Low | None | List application aliases and registered launcher paths. |
| Windows | `windows.setting` | Implemented | Medium | `settings.get`, `settings.set` | Read, set, or toggle supported Windows settings such as dark mode. |

## Implemented Platform Features

- Typed request-result items for files, media candidates, tasks, notes,
  web/news results, and extracted page items; generic selection by number, ID,
  or title; and bounded selected-item context for follow-up actions.
- PySide6 tray UI with a pinned compact companion, expandable assistant,
  settings, skills, permissions, audit, privacy, diagnostics, applications,
  macros, models, and benchmark views. Closing hides to the tray so background
  activation remains available.
- Persisted Dashboard and Assistant primary screens, tray actions and shortcuts
  for each, a responsive one-to-four-column metric grid, manual and per-card
  refresh, and staged add/remove/disable/reorder/resize/title/presentation
  customization.
- Privacy-bounded request, planner, native Ollama model, skill-execution, and
  automation metrics with request/model/execution latency cards; default-deny CPU,
  memory, storage, and battery cards; and read-only Home Assistant entity cards
  with exact origin/entity scopes, encrypted tokens, offline/stale behavior,
  connection health controls, and optional finite-numeric trend retention.
- A live, bounded decision/action/result trace and separate background active
  indicator that do not expose private model chain-of-thought or take over the
  foreground command state.
- Text input, push-to-talk, keyword/clap, persistent voice, and configurable
  local ONNX wake-word activation, plus streaming STT, provider-selectable local
  Piper or offline-lock-aware ElevenLabs TTS, voice-file discovery, cancellation,
  response-detail modes, and privacy controls.
- A separate resizable image/audio/video results window with compact/full
  now-viewing cards,
  image zoom/pan/rotate, Qt audio/video transport controls, typed/voice/keyboard
  actions, source-aware TTS/microphone coordination, fingerprint revalidation,
  and optional verified public-HTTPS downloads in a bounded privacy-clearable
  cache.
- Structured visual responses for local charts, flowcharts, timelines, and
  tables. Visual artifacts use typed local rendering, open in the results
  window, and retain text and speech fallbacks for non-visual clients. A shared
  parser accepts flexible label/value separators, spoken numbers and
  punctuation, timed events, process steps, and column/row cues.
- Deterministic routing plus grounded schema-constrained planning for unmatched
  requests. Result-dependent workflows use staged skill selection and argument
  binding after each typed observation, while ordinary requests retain the
  compact single-completion path. Context collection is demand-driven,
  concurrent, and cached; response budgets and Ollama preload/keep-alive remain
  configurable.
- Default-deny scoped permissions, action-bound confirmations, audit logging,
  emergency stop, rollback where supported, and file access allow/block policy.
- Signed skill-package validation, execution-class enforcement, key revocation,
  update review for new capabilities, macro composition, and offline lock
  enforcement. Downloaded Python packages are blocked by default because the
  available worker boundary is process isolation rather than an OS sandbox.
- A local Extension Hub for searching a configurable HTTPS platform catalog,
  reviewing publisher/risk/permission metadata, and installing compatible
  packages only after digest, archive, trusted-signature, API, and manifest
  verification. Catalog installations remain disabled and permissionless.
- User-authored Markdown playbooks with validated YAML frontmatter, natural
  language workflow instructions, per-skill-ID guidance, enable/disable and
  import/edit/delete controls, semantic retrieval, and unavailable-skill
  grounding. Deterministic text/voice commands support save, list, recall,
  enable/disable, confirmation-gated delete, and exact named application.
  Playbooks guide planning but cannot grant permissions or execute directly.
- Optional MQTT, Home Assistant, Wyoming, web search/fetch, and cloud-model
  integrations behind explicit configuration and permissions.
- Optional Google Calendar integration backed by a shared Google desktop OAuth
  2.0 PKCE account, exact calendar/event permission targets, bounded typed
  results, calendar and event CRUD, recurring instances, RSVP, free/busy,
  colors, settings, and confirmation-gated sharing and Workspace-admin
  ownership transfer.
- Optional Gmail integration backed by the same scope-aware encrypted Google
  account, with scoped message/thread/draft permissions, bounded typed results,
  Gmail-search queries, drafts, replies, sending, labels, read/unread, inbox,
  star, and trash state. Permanent deletion and attachments are excluded.
- Optional Google Drive integration backed by the same encrypted Google account,
  with bounded reads plus exact, non-overwriting download and upload operations.
  Sharing, moves, deletes, and broad synchronization remain excluded.
- Optional Microsoft Graph integrations with a shared, scope-aware desktop OAuth
  boundary. Outlook supports explicit draft/send and exact calendar changes;
  OneDrive supports exact, non-overwriting transfers. Bulk mailbox/calendar
  changes, meeting responses, file moves/deletes, and To Do remain excluded.
- Optional Slack integration with encrypted token storage, bounded reads, and one
  exact confirmed channel or thread post. Public OAuth callback relay, reactions,
  DMs, uploads, and broad ingestion remain excluded.
- Optional Notion integration with encrypted token storage, bounded reads, exact
  page creation, and exact bounded append. Public OAuth, database writes, and
  broad workspace ingestion remain excluded.
- Optional GitHub integration with encrypted token storage, bounded reads, exact
  issue creation, and exact issue comments. Code pushes, workflow/secret changes,
  and bulk repository mutations remain excluded.
- Local bounded document extraction and content indexing for explicitly selected
  files/folders. Index persistence is separate from conversation memory and can
  be inspected or cleared without deleting source files.
- Explicit local-image understanding for OCR, description, charts, and error
  dialogs. It has no screen/camera capture, clipboard, input, or implicit
  retention capability.
- Typed event-triggered automations for exact file, process, device, calendar,
  and public-HTTPS web observations. Built-in polling sources exist for file,
  process, device, and web; calendar observations are supplied only by a
  configured connector. Observations retain bounded metadata or fingerprints,
  not document bodies, web bodies, or private device payloads.
- Resumable ten-step onboarding, categorized recovery guidance, scalable text,
  high contrast, reduced motion, keyboard-accessible controls, and inspect/copy
  source controls for cited responses.
- Versioned deterministic and production-planner evaluation corpora, correlated
  privacy-safe stage metrics, architecture/property tests, coverage gates,
  dependency auditing, CycloneDX SBOM generation, and exact release manifests.
- Optional Playwright headless rendering in a fresh non-persistent context,
  with public-HTTPS-only navigation, bounded result-card extraction, read-only
  request methods, installed Edge/Chrome fallback, and no access to the user's
  browser profile.
- A durable notification Inbox with unread/dismissed state, compact/full unread
  badges, a nonblocking foreground banner and recent-item drawer, exact
  automation-run deep links, quiet hours, and click-through Windows delivery.
- Durable one-time and recurring schedules with SQLite persistence, IANA time
  zones, DST handling, bounded misfire catch-up, multi-instance leases, restart
  reconciliation, durable result notifications, and an Automations control
  center.
- A typed logic layer for immediate random/weighted/rotating choice and durable
  delay, repetition, structured-result conditions, live non-sensitive item
  filtering, retry backoff, no-repeat history, and stop-after-condition,
  stop-after-success, maximum-run, or expiry limits. Nested actions are frozen
  registered skill calls and use the normal authorization and execution path.
- Revocable schedule-scoped background authorization for exact low-risk fixed
  skills and frozen macro plans, with capability/target/risk/runtime/rate bounds,
  skill drift detection, machine-bound restores, locked-session gating, and
  fresh approval for every higher-risk occurrence. Background compatibility is
  checked separately so input, window, clipboard, media, launch/terminate, and
  device-changing capabilities cannot interrupt the active user.

## Not Yet Claimed Complete

The codebase does not claim complete coverage for arbitrary email/calendar
providers, office document editing, spreadsheet editing, slide-deck generation,
live screen/camera OCR, image generation/editing, location/travel, shopping, or
financial workflows. The explicit bounded slices above are implemented; broader
provider and creative/transactional workflows require separate threat and
acceptance reviews.
