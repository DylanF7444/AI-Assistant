# User Guide

## Install and first run

Run `LocalAssistant-0.1.0-Setup.exe`. The installer is per-user and does not
normally require administrator rights. Optional offline model components can be
selected when they are included beside the installer. First run reports CPU,
GPU, memory, audio devices, and recommended model placement.

The first-run guide is a resumable ten-step setup for privacy, microphone,
speech, model, permissions, extensions, accessibility, and diagnostics. A
failed check is categorized and includes recovery guidance; reopening setup
returns to the incomplete step. Open Settings to choose microphone/output
devices, push-to-talk and cancel hotkeys, response detail, speech mode,
retention, privacy, text scale, high contrast, and reduced motion. Enable
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

The default Dashboard contains local Assistant status, request outcomes, typed
request latency, voice request latency, model latency, skill-execution latency,
resource use, automation summary, local tasks, and recent activity cards. The Tasks card shows open and
completed counts plus task titles and status. Select
**Customize** to add, remove, hide, reorder, resize, rename, or change the
presentation and refresh interval of a card. Changes are staged until **Save**;
**Cancel** leaves the saved layout untouched, and **Reset to defaults** restores
the ten local cards. The catalog also offers CPU, memory, storage, and battery
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

The assistant also understands bounded logic requests. Immediate requests such
as `pick one at random from soup, tacos, and pasta` use the local choice skill.
Unseeded choices use fresh operating-system entropy, while retrying the same
execution keeps its checkpointed result. Stateful strategies automatically use
a stable history for the same option set, so `choose from red, green, or blue
using least-recent selection and avoid the last choice` continues rotating even
when no explicit history name is supplied. An explicit phrase such as `remember
the history as colors` gives that history a reusable name.

Immediate probability requests use the chance skill. For example, `give me a 20
percent chance of a Surprise notification and an 80 percent chance of doing
nothing` requests notification permission once, rolls immediately, and treats
the non-triggering branch as an intentional no-op. It does not require a second
condition or event. A future or recurring probability can schedule that same
chance skill.

Scheduled requests can combine one check, a condition, a selection, and an
action, for example `every morning choose an open task I have not picked
recently and notify me; stop after five runs` or `every five minutes calculate
6 * 7 and notify me if the result is above 40, then stop`. `in ten minutes`
creates a durable one-time trigger; interval and calendar phrases use the same
scheduler as reminders.

Logic schedules always have a hard run bound (100 by default, up to 10,000) and
may stop earlier after the condition becomes true, after a specified number of
successful actions, or at an expiry time. Random choices are retry-stable and
may use weights, a seed, recent-choice exclusion, round-robin, or least-recent
selection. A check can contribute selectable items only when its skill returns
non-sensitive structured result items; sensitive items are never copied into
the choice layer. Notification templates expose only the selected ID, label,
and value plus the check status, summary, and item count.

Creation of standing background authority still receives normal review. Every
nested action is a concrete registered skill call frozen when the schedule is
created, so later model output cannot change it. You can inspect, pause, run,
or stop these schedules with the same Automations commands and controls used
for ordinary reminders.

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

### Event-triggered automations

An action schedule may use one exact typed observation instead of a clock. The
built-in sources can watch an approved folder for a created, modified, or
deleted filename; an exact process name for start/stop; an exact local device
instance ID for status changes; or one sanitized public HTTPS resource for a
bounded text fingerprint change. A configured calendar connector may submit
typed calendar observations. For example: `when a PDF is created under
C:\Incoming, notify me that a PDF arrived`.

The assistant shows both the monitor target and frozen action before enabling
the schedule. Monitor grants use separate `automation.observe.*` capabilities;
they do not grant permission to read document bodies, keep web response bodies,
inspect process command lines, or collect private device payloads. Web targets
discard credentials, query strings, and fragments before persistence. Event
sources are polled only while the per-user app is running and remain subject to
minimum intervals, deduplication, ordinary run bounds, offline lock, and the
same execution and approval path as time-based schedules.

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
Calendar scope so the same shared Google account can perform explicitly approved
reads and changes. Disable the global offline lock, return to the Google Calendar
entry in **Extension Hub**, choose **Connect Account**, and select **Sign in with
Google**. The first Google extension you connect establishes the shared account;
Gmail and Drive reuse it and request only their additional product scopes.
The bundled client ID is used by default. If Google requires a client secret for
your Cloud project, expand **Configure OAuth client credentials**, enter the
secret once, and retry. Local Assistant stores the client configuration in the
Windows-encrypted secret vault and reuses it for future sign-ins and token
refreshes; it is never included in settings exports or source code. Developers
can use a different Desktop OAuth client from the same settings.

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

### Gmail setup

Gmail is an optional first-party Extension Hub component. Open **Extension
Hub**, select **Gmail**, choose **Install Extension**, then connect the account
from the Gmail detail page. Review the declared `integration.read`,
`integration.write`, `messaging`, and `network` capabilities before enabling
the skill. Gmail uses the shared Google account established by Calendar or
Drive when one is already connected, and requests only its additional Gmail
scope. A single encrypted Google authorization is shared by all three Google
extensions.

The connection requests Google's `gmail.modify` scope. It supports reading and
searching messages and threads, listing labels and drafts, composing drafts,
sending or replying to email, changing labels, marking read or unread,
archiving, moving mail into the inbox, starring, and moving messages or threads
to and from trash. It intentionally does not request the broader Gmail scope
needed to bypass trash and permanently delete mail, and it does not send or
download attachments.

Common requests such as “check my Gmail,” “show my unread emails,” “find email
from person@example.com,” and fully specified “send an email to … subject …
saying …” commands route locally without a language model. Reading, mailbox
changes, and sending use separate exact-target permission checks. Sending and
replying are high-risk actions and remain subject to the normal confirmation
flow. The global offline lock blocks Gmail API calls and OAuth refreshes.

### Google Drive setup

Google Drive is an optional first-party Extension Hub component. Open
**Extension Hub**, select **Google Drive**, choose **Install Extension**, then
connect the account from the Google Drive detail page. Review the declared
`integration.read` and `network` capabilities before enabling the skill.
Drive uses the shared encrypted Google account authorization used by Calendar
and Gmail. If Drive is the first Google extension connected, its sign-in becomes
the account used by the other Google extensions; later connections request only
the missing Drive, Calendar, or Gmail scope.

Disconnecting Google from any of these extension pages revokes the shared
authorization and signs out Calendar, Gmail, and Drive together. Switching to a
different Google account requires disconnecting the current shared account first.

Drive supports searches such as “find my Drive for quarterly planning,” listing
folders, and inspecting metadata. It can also download one exact Drive item to
an exact new local path or upload one explicit local file to an exact Drive
parent. Transfers are high risk, non-retryable, and never overwrite an existing
local or remote file. Reads, remote writes, local-content reads, and local writes
have separate exact-target permissions. Moves, sharing changes, deletes, and
broad synchronization are not supported. The global offline lock blocks Drive
API calls and OAuth refreshes.

### Microsoft Graph setup

Outlook Mail, Outlook Calendar, and OneDrive are optional first-party Extension
Hub components backed by one Microsoft Graph connection boundary. Install the
desired entry from **Extension Hub**, review its declared capabilities, then
connect the account from that extension's detail page. Each extension requests
only the scopes needed for its read and optional exact write operations, while
the encrypted Microsoft authorization is shared by the three extensions.

Microsoft requires an application registration. Expand **Configure OAuth client
credentials** in the connection dialog and enter a Microsoft Entra desktop/public
client ID before signing in. A client secret is normally not required for a
public desktop client; if the provider requires one, it is stored encrypted and
reused for future token refreshes. Access and refresh tokens never enter
extension package code, transcripts, or the language model.

Graph supports bounded mail, calendar, and OneDrive reads. Outlook Mail can
create one explicit draft and can send only one exact existing draft as a
separate high-risk confirmed action. Outlook Calendar can create, update, or
delete one exact event after showing the target. OneDrive can download one exact
item to a new local path or upload one explicit local file without overwriting.
Bulk mail/calendar changes, message deletion, meeting responses, OneDrive
moves/deletes, and Microsoft To Do remain excluded. The global offline lock
blocks Graph API calls and OAuth refreshes.

### Slack setup

Slack is an optional first-party Extension Hub component. Install **Slack** from
**Extension Hub**, review its `integration.read`, `messaging`, and `network`
capabilities, then open **Connect Account** and enter a Slack token with only
the read and posting scopes required by the operations you intend to use. The token is encrypted
for the current Windows account and never enters extension package code,
transcripts, or the language model.

Slack can search messages, list accessible conversations, read a bounded channel
history or selected thread, and post one exact message to an exact channel or
thread after showing the text and target for confirmation. Automatic DMs,
reactions, file uploads, webhooks, and broad background ingestion are not enabled. Public OAuth
installation remains a follow-up after a reviewed HTTPS callback relay is
available for the desktop app.

### Notion setup

Install **Notion** from **Extension Hub**, then connect a Notion internal
integration token or personal access token configured with only the capabilities
you intend to use. Share the specific pages or data sources with the integration in
Notion; a token alone does not grant access to unshared pages. The encrypted
token stays in the Windows secret vault.

Notion searches shared pages and data sources, inspects an exact ID, and reads
bounded child blocks. It can create one page under an exact parent or append
bounded content to one exact page after confirmation. General edits, database
writes, public OAuth onboarding, and workspace-wide ingestion remain excluded.
The global offline lock blocks Notion API calls.

### GitHub setup

Install **GitHub** from **Extension Hub**, then connect a fine-grained personal
access token with Metadata and the minimum issue access needed for the
repositories and operations you intend to use. The token is encrypted for the current Windows account and
is not included in settings exports or assistant context. A narrower GitHub App
authorization flow is planned for a later release.

GitHub searches repositories and issues, lists repositories for the account,
inspects an exact repository or issue, finds pull requests, and checks releases.
It can create one issue in an exact repository or add one confirmed comment to
an exact issue. Code pushes, workflow or secret changes, branch deletion, and
bulk repository mutations are not supported. The global offline lock blocks
GitHub API calls.

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
When a response includes citations, use **Inspect sources** to review sanitized
source metadata and **Copy sources** to copy the bounded citation list. Local
document citations use opaque document references and page/sheet/slide/line
locators rather than exposing private document text in audit records.
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

## Visual responses

The assistant can create local charts, flowcharts, timelines, and tables in the
same results window. Ask in ordinary language; these are examples, not command
formats:

- `Can you chart monthly sales for January 12, February 18, and March 15?`
- `Draw the release flow: plan, then build, then test, then deploy.`
- `Show my morning timeline—nine AM standup, ten AM build, two PM review.`
- `Make a table with columns name and status, and rows Alice ready, Bob waiting.`

Punctuation is optional. For chart label/value pairs, `January=12`,
`January: 12`, `January is 12`, `January at 12`, and `January twelve` are
equivalent. Dictation can use words such as “then,” “columns,” “rows,” “AM,”
and “PM” as structural cues. Spoken numbers are converted to numeric values.
When a transcript does not contain enough information to infer a boundary—for
example, multiword table cells with no column or row cues—the assistant asks a
clarifying question.

The visualization capability itself accepts either natural language or a typed
visual specification. This lets conversational requests, voice transcripts,
deterministic routing, and planner-created visuals share the same validation
and rendering path.

Visual requests that depend on current public facts can use a permission-gated
skill chain. For example, `Show me a table of the highest-grossing movies this
year` can search for a current source, read one result, and then build the table
from the observed values. Each network skill still requests its normal scoped
permission, and the table is not considered complete until the local
visualization skill succeeds.

Visuals are generated from bounded typed data, require no network or file
permission, and keep an accessible text description in the conversation and
speech paths. Existing `show` and `play` media commands continue to use the
media viewer. See `VISUAL_CAPABILITIES_IMPLEMENTATION_PLAN.md` for the roadmap.

## Documents and image understanding

Use one to five explicit local document paths to extract text, summarize,
compare, find action items, or answer a question. Supported bounded formats are
TXT, Markdown, CSV, PDF, DOCX, XLSX, and PPTX; optional libraries are required
for PDF and Office formats. Results include source locators such as line, page,
sheet, or slide. Encrypted, oversized, unsupported, or malformed files fail with
an actionable category instead of falling back to an external application.

Content search works only within one approved folder. Building an index asks
separately for `filesystem.content.read` and `filesystem.content.index`, records
content signatures so changed/deleted files reconcile on later runs, and never
changes the source documents. Inspect or clear the index explicitly from the
same skill; clearing it does not delete source files.

For one explicit local image, ask to read its text, describe it accessibly,
explain a chart, or explain an error dialog. OCR uses the optional local
Tesseract executable. The image skill does not capture the screen or camera,
read the clipboard, click, type, or retain the image implicitly. Live screen
vision and image generation/editing are outside this release candidate.

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
permission and confirmation normally. To remove it, select the installed entry
in **Extension Hub** and choose **Uninstall Extension**; the package is removed
locally and all runtime skill registrations for it are cleared. See
`docs/EXTENSION_HUB.md` for the platform JSON contract and package requirements.

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
