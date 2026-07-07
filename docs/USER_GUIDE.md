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

Use the cancel hotkey or tray Cancel command to stop capture, transcription,
planning, speech, plans, and active jobs. File deletion uses the Recycle Bin
where Windows supports it. Dry-run previews are available for file operations.
File search results are numbered; after searching, use commands such as
`select 1`, `open it`, `copy it to Desktop`, or `delete it` to act on the
selected result with exact-path permissions and confirmation.

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

## Skills and diagnostics

Install only signed skill packages from trusted publishers. Updates that add a
capability require renewed review. The Skills tab supports enable, disable,
update, uninstall, and health checks. Macros can compose registered skills but
do not bypass any step's validation, permission, confirmation, or audit.
See `docs/FEATURES.md` for the current built-in feature catalog and project
status.

The Diagnostics tab reports model, microphone, GPU, audio, database, and skill
health with recovery guidance. Exported diagnostic bundles redact secrets and
are created only when requested.

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
