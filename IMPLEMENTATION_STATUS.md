# Implementation Status

Updated July 10, 2026. The product checklist is the specification; this file
records implementation evidence and release gates that cannot be honestly
closed by source code or unit tests alone.

## Implemented and Verified

- Python 3.12/PySide6 architecture with typed provider boundaries, event-driven
  orchestration, Windows adapters, testing/production profiles, and SQLite
  schema version 3 migrations, WAL, recovery, backup, and corruption checks.
- Immutable Pydantic contracts for audio, transcripts, intents, manifests,
  calls, plans, results, confirmations, responses, and pipeline events.
- Responsive tray UI with push-to-talk/cancel hotkeys, pipeline states, partial
  transcripts, history, settings, skills, permissions, audit, privacy,
  diagnostics, applications, macros, models, and benchmark views.
- Audio capture with device enumeration, 16 kHz mono PCM, pre-roll, metering,
  VAD/end-of-utterance, timeouts, noise gate, gain control, playback
  suppression, opt-in diagnostic recordings, and actionable device errors.
- Lazy faster-whisper CUDA/CPU STT, whisper.cpp fallback, streaming partial and
  final transcripts, confidence/word timing, aliases, normalization, and
  low-confidence clarification for sensitive targets.
- Deterministic routing plus a schema-constrained local Ollama planner,
  relevant-skill retrieval, bounded session context, cancellation, timeout,
  malformed/unavailable-call rejection, and no raw shell execution.
- Versioned skill API with typed schemas, lifecycle operations, progress,
  health, cancellation, rollback contracts, signed Ed25519 packages,
  capability-expansion review, isolated workers, and Windows Job limits.
- Default-deny scoped permissions, one-time/session/always/deny grants,
  immutable expiring confirmations, critical typed tokens, dry runs, audit,
  idempotency, safe-only retries, dependency-aware plans, and emergency stop.
- Windows skills for approved application launch/window control, media/volume,
  settings, notifications, file search/open/copy/move/rename/recycle,
  numbered file-result selection, contextual selected-file actions, clipboard,
  restricted input automation, sensitive-field detection, device control, and
  process inspection/termination.
- Timer, reminder/alarm, tasks, notes, conversion, calculator, help,
  diagnostics, file access allow/block policy, registered application aliases,
  and permission-composed macros.
- Local Piper TTS adapter with voice/model selection, speed, volume, output
  device, phrase caching, playback suppression, cancellation, and text fallback.
- Signed/expiring device contracts, pairing, replay prevention, revocation,
  MQTT mTLS, Home Assistant HTTPS, and Wyoming TLS adapters.
- Global offline lock, worker network denial, no telemetry, redacted structured
  logs, metrics, privacy-safe diagnostics, local model installation from signed
  removable/HTTPS packages, and signed manual application updates.
- Reproducible PyInstaller executable and Inno Setup installer with selectable
  model components, per-user install, startup task, upgrade preservation, and
  data/model uninstall choices. Install, packaged launch, and file-removal
  uninstall behavior were exercised on the target non-administrator account.
- 428 automated regression tests and 29 guided learning tests pass, including
  offscreen Qt coverage, a 1,500-command sustained run, bounded
  idempotency state, database integrity, isolated outbound-network denial,
  migration, update traversal, permission, confirmation, Windows adapter,
  device, model, audio, planner, and end-to-end text command coverage.

## Current Feature Catalog

The normalized built-in feature catalog is maintained in `docs/FEATURES.md`.
That file records each implemented built-in skill by area, stable skill ID,
status, risk level, permissions, and user-facing capability.

## Measured Target Evidence

- Hardware: Ryzen 5 5600G, 15.8 GB RAM, GTX 1660 Super 6 GB, Windows 11,
  Python 3.12.6.
- Deterministic command benchmark: 100 iterations, 25.0 ms median, 28.7 ms
  p95, 50.9 ms maximum, 603,153 Python peak bytes.
- Ollama `qwen2.5:3b`: 3/3 initial structured calls correct, 1.71 s median,
  approximately 68-84 generated tokens/second.
- Packaged artifacts: `dist/LocalAssistant.exe` and
  `dist/LocalAssistant-0.1.0-Setup.exe`.
- Temporary-profile desktop smoke: startup, all 14 tabs, `calculate 17 times
  19`, displayed result `323`, and clean shutdown.

## External Release Gates

- Run the supplied recorded-audio benchmark with a licensed local corpus that
  covers accents, noise, silence, interruption, and low-confidence targets;
  select the final Whisper model from measured accuracy/real-time factor.
- Configure a real Piper voice and measure first-audio latency, pronunciation,
  device switching, speaker echo behavior, and repeated barge-in.
- Perform an interactive microphone-to-command acceptance run and a fresh
  Windows-account/machine installer run. Automated tests cannot prove physical
  microphone, speaker, hotkey, or desktop focus behavior.
- Authenticode-sign the executable and installer with a publisher certificate
  and protected private key. No signing certificate is present in this
  workspace, so public-distribution signing cannot be fabricated.
- For hostile third-party native code, add an OS security boundary such as
  AppContainer/Windows Sandbox. The current signed subprocess, offline lock,
  timeout, and Job limits isolate failures and resources but are not a complete
  security sandbox.

These gates block a truthful public-release or complete MVP acceptance claim;
they do not represent unfinished application code that can be supplied without
the external assets, certificate, or manual hardware environment.
