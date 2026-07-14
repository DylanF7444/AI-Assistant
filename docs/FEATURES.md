# Feature Catalog

Updated July 10, 2026. This catalog lists the built-in features registered by
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
| Core | `core.reminder` | Implemented | Low | None | Create local reminders or alarms. |
| Core | `core.tasks` | Implemented | Low | None | Add, list, complete, and delete local tasks. |
| Core | `core.timer` | Implemented | Low | None | Run a cancellable local timer. |
| Core | `core.unit_conversion` | Implemented | Low | None | Convert common distance, weight, volume, and temperature units locally. |
| Network | `cloud.ask` | Optional | Low | `network`, `process.launch` for Codex | Ask configured OpenAI, Anthropic, or local Codex CLI providers. |
| Network | `web.fetch` | Optional | Low | `network` | Read text from a specific public HTTPS page. |
| Network | `web.search` | Optional | Low | `network` | Search the public web through DuckDuckGo Instant Answer or Brave Search. |
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

- PySide6 tray UI with a pinned compact companion, expandable assistant,
  settings, skills, permissions, audit, privacy, diagnostics, applications,
  macros, models, and benchmark views. Closing hides to the tray so background
  activation remains available.
- Text input, push-to-talk, keyword/clap, persistent voice, and configurable
  local ONNX wake-word activation, plus streaming STT, optional local Piper TTS,
  cancellation, response-detail modes, and privacy controls.
- Deterministic routing plus schema-constrained local planner support for
  ambiguous or multi-step requests.
- Default-deny scoped permissions, action-bound confirmations, audit logging,
  emergency stop, rollback where supported, and file access allow/block policy.
- Signed skill-package validation, isolated worker execution, update review for
  new capabilities, macro composition, and offline lock enforcement.
- Optional MQTT, Home Assistant, Wyoming, web search/fetch, and cloud-model
  integrations behind explicit configuration and permissions.

## Not Yet Claimed Complete

The codebase does not currently claim complete coverage for email, calendar,
messaging accounts, office document editing, spreadsheet editing, slide deck
generation, screen OCR, image generation/editing, location/travel, shopping, or
financial workflows. The framework can be extended with skills for those areas,
but they are not built-in implemented features in this repository.
