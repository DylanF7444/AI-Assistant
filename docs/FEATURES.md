# Feature Catalog

Updated August 2, 2026. This catalog lists the built-in features registered by
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
| Core | `core.datetime` | Implemented | Low | None | Return the local date, time, or both. |
| Core | `core.diagnostic` | Implemented | Low | None | Report local hardware, microphone, model, and skill health. |
| Core | `core.file_access_policy` | Implemented | High | `settings.get`, `settings.set` | List and change filesystem read and write/delete allow and block lists. |
| Core | `core.help` | Implemented | Low | None | Explain available commands, skills, and required permissions. |
| Core | `core.macro` | Extensible | Low | Per macro step | Run an existing configured sequence by its exact saved name without bypassing validation, permissions, or confirmations. |
| Core | `core.notes` | Implemented | Low | None | Add, search, list, and delete local notes. |
| Core | `core.playbook` | Extensible | Dynamic | None | Save, list, show/recall, enable, disable, or confirmation-gated delete of Markdown planning playbooks. |
| Core | `core.reminder` | Implemented | Low | None | Create local reminders or alarms. |
| Core | `core.schedule` | Implemented | Dynamic | None | Create recurring reminders or reviewed frozen actions/macros; list, inspect, pause, resume, revise, delete, snooze, run now, and inspect or clear run history. |
| Core | `core.tasks` | Implemented | Low | None | Add, list, complete, and delete local tasks. |
| Core | `core.timer` | Implemented | Low | None | Run a cancellable local timer. |
| Core | `core.unit_conversion` | Implemented | Low | None | Convert common distance, weight, volume, and temperature units locally. |
| Network | `cloud.ask` | Optional | Low | `network`, `process.launch` for Codex | Ask configured OpenAI, Anthropic, or local Codex CLI providers. |
| Network | `web.fetch` | Optional | Low | `network` | Read text from a specific public HTTPS page. |
| Network | `web.headless_read` | Optional | Low | `network` | Render a public HTTPS page in a fresh non-persistent headless browser and return bounded visible text/links or matching deal, product, article, listing, and result cards without opening a user window. |
| Network | `web.news_search` | Optional | Low | `network` | Retrieve current headlines with source and publication time for reports and briefings. |
| Network | `web.search` | Optional | Low | `network` | Search the public web through DuckDuckGo or Brave Search. |
| Connections | `integration.google_calendar` | Extension Hub/Optional | Dynamic | `integration.read`, `integration.write`, `network` | Install the first-party Hub extension, connect Google with in-app encrypted OAuth, and list or manage calendars, events, availability, invitations, settings, and sharing rules. |
| Connections | `integration.gmail` | Extension Hub/Optional | Dynamic | `integration.read`, `integration.write`, `messaging`, `network` | Install the first-party Hub extension, connect Google with in-app encrypted OAuth, and search or read messages and threads, manage drafts and mailbox state, and send explicitly approved email. |
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
- Deterministic routing plus a single grounded schema-constrained planner
  completion for unmatched requests, demand-driven concurrent cached context,
  proportional response budgets, and configurable Ollama preload/keep-alive.
- Default-deny scoped permissions, action-bound confirmations, audit logging,
  emergency stop, rollback where supported, and file access allow/block policy.
- Signed skill-package validation, isolated worker execution, update review for
  new capabilities, macro composition, and offline lock enforcement.
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
- Optional Google Calendar integration with desktop OAuth 2.0 PKCE, encrypted
  refresh/access tokens, exact calendar/event permission targets, bounded typed
  results, calendar and event CRUD, recurring instances, RSVP, free/busy,
  colors, settings, and confirmation-gated sharing and Workspace-admin
  ownership transfer.
- Optional Gmail integration with desktop OAuth 2.0 PKCE, separately encrypted
  Gmail tokens, scoped message/thread/draft permissions, bounded typed results,
  Gmail-search queries, drafts, replies, sending, labels, read/unread, inbox,
  star, and trash state. Permanent deletion and attachments are excluded.
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
- Revocable schedule-scoped background authorization for exact low-risk fixed
  skills and frozen macro plans, with capability/target/risk/runtime/rate bounds,
  skill drift detection, machine-bound restores, locked-session gating, and
  fresh approval for every higher-risk occurrence. Background compatibility is
  checked separately so input, window, clipboard, media, launch/terminate, and
  device-changing capabilities cannot interrupt the active user.

## Not Yet Claimed Complete

The codebase does not currently claim complete coverage for email, calendar,
messaging accounts, office document editing, spreadsheet editing, slide deck
generation, screen OCR, image generation/editing, location/travel, shopping, or
financial workflows. The framework can be extended with skills for those areas,
but they are not built-in implemented features in this repository.
