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
to restore it. Use **Open Full Assistant** for the management interface,
**Emergency Stop** to cancel active work, and **Quit** to stop the assistant.
Quit also stops on-time reminder and automation delivery until the app starts
again. Select the installer's **Start Local Assistant with Windows** option when
you want schedules reconciled automatically after sign-in.

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

## Local models

- STT: install a faster-whisper model locally or configure a whisper.cpp
  executable and model. CUDA falls back to CPU when unavailable.
- Planner: install Ollama locally and select an installed quantized model.
  Core deterministic commands continue to work without it.
- Speech: configure a Piper executable and ONNX voice. Text remains available
  whenever speech is disabled or unavailable.

The Models tab can install signed packages from removable media. HTTPS download
is an explicit optional action. Model packages are verified before activation.

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
File search results are numbered; after searching, use commands such as
`select 1`, `open it`, `copy it to Desktop`, or `delete it` to act on the
selected result with exact-path permissions and confirmation.
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

## Microphone and privacy

Use the microphone test and level meter before voice commands. Raw audio is not
stored unless diagnostic recording is explicitly enabled. Transcript retention
is configurable. Privacy controls separately clear transcript history, session
memory, model cache, TTS cache, diagnostic audio, and audit records.

Core features have no telemetry or cloud account. The offline lock blocks
outbound connections, including isolated skill workers. MQTT, Home Assistant,
Wyoming, and HTTPS model download are optional network features and must be
configured explicitly.

Web and cloud-model skills are optional. Disable the global offline lock to use
DuckDuckGo Instant Answer for keyless web searches. DuckDuckGo is the default;
Brave Search remains selectable in Settings and requires a Brave Search API
key. Configure an OpenAI or Anthropic API key to ask ChatGPT/OpenAI models or
Claude. Codex requests use the locally installed `codex exec` CLI. API keys are
encrypted with Windows DPAPI and are not included in settings exports. Cloud
API invocations require the `network` permission; Codex invocations require
`network` and `process.launch` for the Codex CLI. Supported commands include
`search the web for ...`, `read https://...`, `ask ChatGPT ...`, `ask Codex ...`,
`ask Claude ...`, and `make code to ...`.

For JavaScript-rendered pages, install the optional browser extra and Chromium:

```powershell
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

Then use commands such as `read https://example.com in the background` or
`render https://example.com with a headless browser`. The assistant uses a new
private, non-persistent browser context and never attaches to the user's visible
browser or normal profile. This initial headless capability is read-only.

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
