# User Guide

## Install and first run

Run `LocalAssistant-0.1.0-Setup.exe`. The installer is per-user and does not
normally require administrator rights. Optional offline model components can be
selected when they are included beside the installer. First run reports CPU,
GPU, memory, audio devices, and recommended model placement.

Open Settings to choose microphone/output devices, push-to-talk and cancel
hotkeys, response detail, speech mode, retention, and privacy options. Register
applications by friendly spoken name before asking the assistant to launch
them; arbitrary executable paths are not accepted from transcripts or models.

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

## Microphone and privacy

Use the microphone test and level meter before voice commands. Raw audio is not
stored unless diagnostic recording is explicitly enabled. Transcript retention
is configurable. Privacy controls separately clear transcript history, session
memory, model cache, TTS cache, diagnostic audio, and audit records.

Core features have no telemetry or cloud account. The offline lock blocks
outbound connections, including isolated skill workers. MQTT, Home Assistant,
Wyoming, and HTTPS model download are optional network features and must be
configured explicitly.

## Skills and diagnostics

Install only signed skill packages from trusted publishers. Updates that add a
capability require renewed review. The Skills tab supports enable, disable,
update, uninstall, and health checks. Macros can compose registered skills but
do not bypass any step's validation, permission, confirmation, or audit.

The Diagnostics tab reports model, microphone, GPU, audio, database, and skill
health with recovery guidance. Exported diagnostic bundles redact secrets and
are created only when requested.
