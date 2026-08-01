# User Guide

## Install and first run

Run `LocalAssistant-0.1.0-Setup.exe`. The installer is per-user and does not
normally require administrator rights. Optional offline model components can be
selected when they are included beside the installer. First run reports CPU,
GPU, memory, audio devices, and recommended model placement.

Open Settings to choose microphone/output devices, push-to-talk and cancel
hotkeys, response detail, speech mode, retention, and privacy options. Enable
Show request breakdown to include the classification, selected skill, arguments,
and execution steps under each on-screen response. Register applications by
friendly spoken name before asking the assistant to launch them; arbitrary
executable paths are not accepted from transcripts or models.

## Compact companion

The assistant opens as a compact companion on first launch and remembers whether
the compact or full interface was last used. Compact mode stays above other
windows by default; use **Pinned** to turn that behavior off and **Expand** to
open the full settings and management interface. It shows the latest request,
assistant response, live voice transcript, microphone level, and pipeline status.

Type a request and press Enter to send it, or press Shift+Enter for a new line.
The voice menu switches immediately between hotkey-only, keyword/clap, and
persistent listening. Model-based wake-word monitoring is configured separately
in full Settings by selecting a local openWakeWord ONNX model and sensitivity.
Wake-word, microphone, model, and global-hotkey changes require an application
restart.

Closing the window hides it to the system tray while voice activation and global
hotkeys continue running. Double-click the tray icon or choose **Show Companion**
to restore it. Use **Open Dashboard** or **Open Assistant** to open either
primary full-window screen, **Emergency Stop** to cancel active work, and
**Quit** to stop the assistant.
Quit also stops on-time reminder and automation delivery until the app starts
again. Select the installer's **Start Local Assistant with Windows** option when
you want schedules reconciled automatically after sign-in.

## Dashboard

The full window presents Dashboard and Assistant as its two primary screens.
The last selected primary screen is remembered when the window is hidden and
restored. Use `Ctrl+1` for Dashboard and `Ctrl+2` for Assistant.

The default Dashboard contains local Assistant status, request outcomes,
end-to-end request latency, model latency, skill-execution latency, automation
summary, and recent activity cards. Select
**Customize** to add, remove, hide, reorder, resize, rename, or change the
presentation and refresh interval of a card. Changes are staged until **Save**;
**Cancel** leaves the saved layout untouched, and **Reset to defaults** restores
the seven local cards. The catalog also offers CPU, memory, storage, and battery
cards. These remain unavailable until the exact read-only `device.read` grant
is approved from the card.

Select **Connections** to add a Home Assistant base URL and long-lived token,
test or disable the connection, opt into numeric history, clear its retained
samples, or remove it. Tokens are kept in the Windows-encrypted secret vault.
After connecting, add a Home Assistant card and enter one exact entity ID plus
an optional scalar attribute. The first refresh asks for `integration.read` on
that entity and `network` on the configured origin. Dashboard cards never call
Home Assistant services or change devices.

Each card shows its source, last update, and ready, stale, permission, offline,
empty, or error state. Per-card refresh intervals run only while Dashboard is
visible; **Refresh** updates all cards immediately while preserving permissions,
offline checks, timeouts, and concurrency limits. External failures retain the
last successful value as stale. The global offline lock stops external
collection before a network request.

External trend storage is off by default and accepts only finite numbers—never
entity text, attributes, response bodies, or credentials. Configure the
1–90-day retention in Settings. Clear a connection's samples from
**Connections**; removing the connection also removes its cards, token, grants,
and history. Dashboard refresh, timeout, stale, permission, offline, error, and
sample counters are shown under Diagnostics.

## Reminders and automations

Commands such as `remind me in twenty five minutes to check the oven` create a
durable one-time reminder. Recurring forms include `every weekday at 8:30 AM
remind me to review my tasks` and `every 2 hours remind me to stretch`. Use
`list my schedules`, `pause schedule NAME`, `resume schedule NAME`, `run schedule
NAME now`, `snooze schedule NAME for ten minutes`, or `delete schedule NAME` to
manage them. Calendar schedules use the configured IANA time zone and preserve
their local wall-clock time across daylight-saving changes.

The full **Automations** tab shows live jobs, active/paused/pending schedules,
their next run and risk, pending approvals, and recent run history. From there
you can pause or resume one schedule, run it now, revoke its background
authority, delete it, review exact higher-risk work, reject or approve an
occurrence, clear completed history, or persistently pause all automations.
Emergency Stop applies an immediate volatile pause before canceling active work;
use **Resume all** when it is safe to continue.

Every reminder, approval request, failure, completed automation, and background
report is also recorded in the durable **Inbox**. The unread count appears on
the Inbox tab and on the compact/full Assistant buttons. Open the notification
drawer for a quick preview, or open Inbox to mark items read, dismiss them, and
follow an item to its exact automation run. Completed runs with displayable
output open in a separate results window, so a daily report does not replace the
active conversation.

When the Assistant has focus, eligible notifications use a nonblocking in-app
banner. When it is hidden or unfocused, they use a Windows notification whose
click opens the stored item. Both are presentation layers over the same durable
Inbox record. Settings can independently disable banners or Windows
notifications and configure overnight quiet hours. Quiet hours suppress
ordinary interruptions but never discard Inbox entries; errors may still
interrupt so failures are not silently missed.

Settings keeps durable reminders enabled by default while background skill
actions are off by default. Enabling background actions does not grant broad
authority: each fixed action or frozen macro is separately reviewed and bound to
its exact schedule revision. Low-risk work may then run unattended. Medium,
high, and critical occurrences always wait for approval, and critical approval
requires the final six characters of the displayed confirmation ID.

The Assistant page keeps a live decision and action trace and shows whether
background work is active. Background events do not replace the foreground
status or disable foreground command controls. Actions that send input, focus
or launch windows, use the clipboard, control media, terminate processes, or
change display/network/power/device state are rejected as unattended work even
while Windows is unlocked. File and database work, read-only device queries,
web APIs, and explicitly background-safe integrations can remain eligible.

Closing the window to the tray keeps scheduling active. Sleep, restart, or an
ordinary crash is handled on next start according to the schedule's missed-run
policy. The assistant does not run while the computer is powered off and does
not wake it from sleep. If a skill, target, policy, schema, permission, risk, or
machine identity changes, the occurrence stops and the schedule may require
review rather than silently expanding its authority.

## Voice and local models

- STT: install a faster-whisper model locally or configure a whisper.cpp
  executable and model. CUDA falls back to CPU when unavailable.
- Planner: install Ollama locally and select an installed quantized model.
  Core deterministic commands continue to work without it. **Local LLM
  keep-alive** controls how long Ollama keeps the selected model loaded after a
  request (`30m` by default), and **Preload local LLM at startup** warms it in
  the background so the first conversational request avoids model-load delay.
- Speech: choose local Piper or cloud ElevenLabs in Settings. Text remains
  available whenever speech is disabled or unavailable.

Unmatched conversational requests use one grounded model completion to
interpret the goal and answer or select skills. Context providers are selected
from the request and candidate skills and collected concurrently; simple
factual questions use a short answer budget, while explanation and settings
requests retain larger budgets. The request, planner, model, and execution
timings are local metrics and do not store prompt or response text.

The Models tab can install signed packages from removable media. HTTPS download
is an explicit optional action. Model packages are verified before activation.

### Voice setup

For fully local speech, select **Piper (local)** as the speech provider. Use the
**Browse** button beside **Piper executable** to select `piper.exe`, then use the
voice-model **Browse** button to select the voice's `.onnx` file. The file picker
starts in detected application, project, or user-data model folders when
possible. A Piper voice normally has a same-name `.onnx.json` configuration file
beside it; keep both files together. Save Settings and restart the assistant.

For ElevenLabs, select **ElevenLabs (cloud)**, enter an API key, copy a voice ID
from **My Voices**, and choose a model ID. `eleven_flash_v2_5` is the low-latency
default; `eleven_multilingual_v2` favors consistent, high-quality multilingual
speech; `eleven_v3` supports more expressive speech. The key is encrypted in the
Windows secret vault and excluded from settings exports. Save Settings and
restart after changing providers or voice/model IDs.

ElevenLabs sends the response's speech text to the ElevenLabs API and may incur
account usage charges. It cannot make a request while the global offline lock is
enabled. Short generated responses use the local TTS cache; clear that cache
from Privacy when desired.

### Google Calendar setup

Google Calendar is a first-party Extension Hub component rather than an
unconditionally registered built-in skill. It is shown in the Hub even when no
external catalog URL is configured. Open **Extension Hub**, select **Google
Calendar**, choose **Install Extension**, then review and enable it in
**Skills**.

The extension uses Local Assistant's Google Desktop OAuth client and the full
Calendar scope so the same connection can perform explicitly approved reads and
changes. Disable the global offline lock, return to the Google Calendar entry in
**Extension Hub**, choose **Connect Account**, and select **Sign in with Google**.
No client ID or client secret is required. Developers can use a different
Desktop OAuth client from the advanced connection settings.

A successful account connection enables the installed Calendar skill. It is
enabled again automatically after restart while that encrypted account
connection remains present, and disconnecting the account disables it. Common
requests such as “check my calendar,” “what meetings do I have tomorrow,”
“find the planning meeting on my calendar,” and “add dentist tomorrow at 3 to
my calendar” route directly without requiring the local language model.

The assistant opens Google's authorization page in the system browser and
receives the response on a temporary `127.0.0.1` callback protected by OAuth
state and PKCE. Access and refresh tokens are encrypted for the current Windows
user and are not given to extension package code, settings exports, transcripts,
skill arguments, or the language model. The same dialog can test, reconnect, or
disconnect the account.

After enabling and connecting, natural-language requests can list or
search calendars and events, inspect recurring instances, check free/busy,
create or import events, quick-add, edit, move or delete events, answer
invitations, manage secondary calendars and calendar-list preferences, inspect
colors and settings, manage sharing rules, and perform the Google Workspace
administrator-only secondary-calendar ownership transfer. Read permissions and
change permissions are separate. Calendar clearing/deletion, ownership
transfer, and sharing-rule changes are critical actions requiring the displayed
typed confirmation token.

The integration does not create Calendar API push notification channels. Those
require an authenticated public HTTPS webhook receiver, which this local-only
desktop application does not provide. The global offline lock blocks Calendar
API calls and OAuth refreshes.

## Commands, permissions, and confirmation

Skills receive no sensitive permission by default. When a command needs one,
the Assistant tab asks directly and shows the capability, reason, and exact
target. You can grant it once, for the current session, or always without
opening Settings or entering a capability name. The Permissions tab remains
available to review, revoke, deny, or preconfigure grants. Medium, high, and
critical actions also show the exact proposed action immediately before
execution. A confirmation is single-use, expires, and cannot approve changed
arguments.

Use the cancel hotkey or tray Emergency Stop command to stop capture, transcription,
planning, speech, plans, and active jobs. File deletion uses the Recycle Bin
where Windows supports it. Dry-run previews are available for file operations.
Requests can return numbered items that remain available for follow-up work.
This applies to file and media searches, tasks, notes, web/news searches, and
matching page items. Use `select 1`, `select the second one`, an item ID such as
`select task-42`, or an unambiguous title. Later requests such as `complete it`,
`open it`, or `tell me more about it` receive the selected item's typed value
in assistant context rather than relying on the text previously displayed.
File items continue to support commands such as `copy it to Desktop` or
`delete it`, with exact-path permissions and confirmation.
Named subfolders can be searched relative to a known folder, for example
`find my resume in the Assistant folder on my Desktop`. Folder organization
can use explicit extensions or the built-in image and text-file categories.

Settings also includes file access policy lists. Read and write/delete
blacklists block matching paths even when a permission grant exists; whitelists
limit filesystem access to the configured paths. You can review or change the
same lists by command, for example `show file access policy`, after granting
the settings permission.

Some navigation commands use the current desktop context. If a browser is
focused, or if a browser window is visible on any display, commands like these
apply to that browser: `open a new tab`, `close this tab`, `reopen closed tab`,
`next tab`, `previous tab`, `go back`, `go forward`, `refresh the page`, and
`focus the address bar`. Named browser commands such as `open a brave tab`
still launch the registered browser directly.
Relational requests such as `open my browser` use an enabled browser preference
when it names an already registered application; memory never introduces an
unregistered executable path.

## Images, audio, and video

Ask the assistant to show an exact local media path, find a named media file in
an approved folder, or use a numbered file-search result. For example:

- `show C:\Users\Me\Pictures\diagram.png`
- `show the vacation photo in Pictures`
- `pull up the second result`
- `play C:\Users\Me\Music\interview.wav`
- `show https://cdn.example.com/demo.mp4`

Local files request exact `filesystem.read` access. An explicit remote URL
requests `network`, works only when offline lock is off, and is downloaded and
verified before display. Web pages, generic image search, streaming manifests,
DRM services, and YouTube pages are not media sources.

Media opens in a separate, resizable results window instead of compressing or
expanding the Assistant conversation. The window supports image fit, actual
size, zoom, pan, rotation, fullscreen, and close. Audio/video controls include play/pause, stop, seek,
ten-second skip, mute, volume, speed, and timestamps. The compact companion
keeps a now-viewing card with open, play/pause, and close controls. Voice and
typed phrases such as `pause it`, `skip ahead thirty seconds`, `set playback
speed to 1.5x`, `mute this video`, and `close the viewer` affect only this
in-app viewer; use ordinary Windows media commands for another application.

Push-to-talk pauses playing viewer media and resumes it only if the same item
was interrupted without another viewer command. Assistant speech takes
priority, passive listening ignores speaker output, and Emergency Stop stops
playback. Runtime formats, output devices, cache size, and decoder error codes
appear in Diagnostics. See `MEDIA_VIEWER.md` for keyboard controls, supported
workflow boundaries, and the release matrix.

## Microphone and privacy

Use the microphone test and level meter before voice commands. Raw audio is not
stored unless diagnostic recording is explicitly enabled. Transcript retention
is configurable. Privacy controls separately clear transcript history, session
memory, model cache, TTS cache, downloaded media cache, diagnostic audio, and
audit records. Clearing downloaded media closes an active remote presentation
but never deletes a local source file.

Core features have no telemetry or cloud account. The offline lock blocks
outbound connections, including isolated skill workers. MQTT, Home Assistant,
Wyoming, and HTTPS model download are optional network features and must be
configured explicitly.

Web and cloud-model skills are optional. Disable the global offline lock to use
DuckDuckGo for keyless web searches and current headline feeds for news
briefings. DuckDuckGo is the default; Brave Search remains selectable in
Settings and requires a Brave Search API key. Configure an OpenAI or Anthropic
API key to ask ChatGPT/OpenAI models or
Claude. Codex requests use the locally installed `codex exec` CLI. API keys are
encrypted with Windows DPAPI and are not included in settings exports. Cloud
API invocations require the `network` permission; Codex invocations require
`network` and `process.launch` for the Codex CLI. Supported commands include
`search the web for ...`, `read https://...`, `ask ChatGPT ...`, `ask Codex ...`,
`ask Claude ...`, and `make code to ...`.

For JavaScript-rendered pages, install the optional browser extra:

```powershell
python -m pip install -e ".[browser]"
```

Then use commands such as `read https://example.com in the background` or
`render https://example.com with a headless browser`. Result-oriented requests
such as `Go to Slickdeals and give me the first deal` also use this skill and
return bounded matching cards with their title, details, and URL. The assistant
uses a new private, non-persistent browser context and never attaches to the
user's visible browser or normal profile. It uses installed Microsoft Edge or
Google Chrome when available; run `python -m playwright install chromium` only
when neither system browser is available. The capability is read-only and blocks
mutating page requests, downloads, service workers, and WebSockets.

## Skills and diagnostics

Install only signed skill packages from trusted publishers. Updates that add a
capability require renewed review. The Skills tab supports enable, disable,
update, uninstall, and health checks. Macros can compose registered skills but
must already exist under their exact saved name and do not bypass any step's
validation, permission, confirmation, or audit. Open-ended requests are
interpreted by the local planner before skill selection. Extension-based file
organization previews every matched move and requires folder-scoped read/write
permissions and confirmation before changing files.
See `docs/FEATURES.md` for the current built-in feature catalog and project
status.

## Extension Hub

The full desktop includes an **Extension Hub** for browsing a curated extension
platform. Enter its public HTTPS catalog URL in **Settings**, turn off the
global offline lock, and restart the application before refreshing the catalog.
Search or select an entry to review its publisher, version, risk, and declared
permissions, then choose **Install Extension**.

The hub currently installs compatible API 1.0 skill packages. It verifies the
catalog digest, safe archive structure, trusted publisher signature, and exact
manifest metadata before copying anything into the local skill directory. An
installed extension remains disabled and has no permission grants. Open
**Skills** to review and enable it; protected actions still request scoped
permission and confirmation normally. See `docs/EXTENSION_HUB.md` for the
platform JSON contract and package requirements.

The Diagnostics tab reports model, microphone, GPU, audio, database, and skill
health with recovery guidance. Exported diagnostic bundles redact secrets and
are created only when requested.

## Playbooks

Use the Playbooks tab to create, edit, import, enable, disable, or delete local
Markdown workflow guides. Files are stored under the configured data
directory's `playbooks` folder, so advanced users can also create or edit them
with a text editor. Invalid files are ignored and reported in the tab.

Each file starts with YAML frontmatter containing a stable `id`, `name`, optional
description and triggers, and an optional `skills` list. A skill entry contains
the registered skill `id` plus natural-language `instructions` describing its
role in the workflow. The Markdown body contains the overall procedure. The New
from Template button provides a complete editable example.

Enabled playbooks are searched for open-ended planner requests. A relevant
playbook guides interpretation and planning, while its available skill IDs are
prioritized as grounded candidates. Missing or disabled skill IDs are shown in
the UI and cannot be selected. Playbooks cannot install skills, grant
permissions, suppress confirmation, add arbitrary shell access, or run directly
as unattended schedules. Every resulting action still passes normal schema,
permission, confirmation, and audit checks.

Playbooks can also be managed by text or voice:

- `save playbook Release Check: First inspect the notes. Then summarize risks.`
- `save these instructions as a playbook called Release Check: First ...`
- `save playbook Release Check using skills core.datetime, core.tasks: First ...`
- `list playbooks`
- `show playbook release-check` or `recall playbook Release Check`
- `enable playbook release-check` and `disable playbook release-check`
- `delete playbook release-check` (requires confirmation)
- `use playbook Release Check to prepare version 2.0`

Saving the same name or ID updates the existing instructions and increments a
numeric version. Existing skill guidance is retained unless the save command
includes a new comma-separated `using skills` list. Explicit `use`, `run`,
`follow`, or `apply` commands bind the exact enabled playbook into planner
context; an unknown or disabled reference is reported without invoking a
different playbook.

## Knowledge bases

Use the Knowledge tab to add a reference note or import a UTF-8 TXT, Markdown,
RST, CSV, or JSON file up to 2 MB. Enabled knowledge bases are searched locally
for each unmatched conversational request, and only the most relevant bounded
excerpts are passed to the local planner. Disable an entry to keep it stored
without using it, or remove entries individually. The Privacy tab can clear all
knowledge bases independently.

Knowledge content is treated as untrusted reference material, not as assistant
instructions. Files are copied into the local database when imported, so later
changes to the original file are not applied until it is imported or edited
again.
