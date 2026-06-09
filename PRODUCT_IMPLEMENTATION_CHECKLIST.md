# Offline Desktop Voice Assistant: Product Implementation Checklist

> Implementation evidence and outstanding release gates are tracked in
> `IMPLEMENTATION_STATUS.md`. Items are not checked here unless their complete
> acceptance evidence, including hardware/manual requirements, exists.

## Product Target

- [ ] Build a Windows desktop application that converts speech into safe, validated commands.
- [ ] Keep speech recognition, intent processing, command execution, and speech synthesis offline by default.
- [ ] Optimize for a Ryzen 5 5600G, GTX 1660 Super, and 16 GB RAM.
- [ ] Target a fast perceived response, with immediate visual feedback and streaming where practical.
- [ ] Support Python-authored skills through a stable, versioned plugin interface.
- [ ] Require explicit permission grants before skills can perform sensitive actions.
- [ ] Design the core so external devices and remote audio endpoints can be added later.

## Recommended Initial Stack

- [ ] Use Python 3.12 or the latest version supported by all selected model libraries.
- [ ] Use PySide6 for the Windows desktop UI and system tray integration.
- [ ] Use `asyncio` for audio, model, and skill orchestration.
- [ ] Use `faster-whisper` with CUDA for primary speech recognition.
- [ ] Provide `whisper.cpp` as a CPU-compatible fallback.
- [ ] Use Silero VAD or an equivalent lightweight local voice activity detector.
- [ ] Start with push-to-talk; add an offline wake-word engine after the core pipeline is stable.
- [ ] Use Ollama or llama.cpp as the local LLM runtime.
- [ ] Select a quantized 3B-7B instruction model that reliably produces structured tool calls within available VRAM.
- [ ] Use a deterministic intent path for common commands so the LLM is not required for every request.
- [ ] Use Piper or a maintained compatible local TTS runtime for initial speech responses.
- [ ] Use Pydantic models and JSON Schema for all command and skill arguments.
- [ ] Use SQLite for settings, permissions, aliases, history, and audit records.
- [ ] Use SQLAlchemy or SQLModel for database access and migrations.
- [ ] Use Windows Credential Manager or DPAPI for locally stored secrets.
- [ ] Use `pytest`, `pytest-asyncio`, and mocked system adapters for automated testing.
- [ ] Package the application with PyInstaller or Nuitka and create a signed Windows installer.

## Repository and Architecture

- [ ] Create clear packages for `audio`, `stt`, `language`, `router`, `skills`, `policy`, `execution`, `tts`, `ui`, `storage`, and `integrations`.
- [ ] Define dependency boundaries so model providers can be replaced independently.
- [ ] Define typed interfaces for STT, LLM, TTS, wake-word, and embedding providers.
- [ ] Define a central event model for pipeline state changes.
- [ ] Keep the UI independent from model and command implementations.
- [ ] Keep operating-system calls behind Windows adapter interfaces.
- [ ] Add dependency injection for providers, storage, clocks, and system adapters.
- [ ] Add configuration profiles for development, testing, and packaged production.
- [ ] Add database schema migrations from the first release.
- [ ] Document architecture decisions and plugin compatibility guarantees.

## Core Data Contracts

- [ ] Define an `AudioSession` model with source, format, sample rate, timestamps, and session ID.
- [ ] Define a `Transcript` model with raw text, normalized text, confidence, language, and word timestamps.
- [ ] Define an `Intent` model with name, arguments, confidence, source, and ambiguity indicators.
- [ ] Define a `SkillManifest` model with ID, version, description, schemas, permissions, risk, and entry point.
- [ ] Define a `SkillCall` model with caller, arguments, permission context, and correlation ID.
- [ ] Define a `SkillResult` model with status, summary, structured data, error details, and rollback metadata.
- [ ] Define a `ConfirmationRequest` model that describes the exact proposed action.
- [ ] Define a `Response` model with display text, speech text, severity, and output preferences.
- [ ] Version all persisted and plugin-facing contracts.

## Desktop Application Shell

- [ ] Create a system tray application that can start with Windows.
- [ ] Add configurable global push-to-talk and cancel hotkeys.
- [ ] Show listening, transcribing, thinking, awaiting approval, executing, speaking, and error states.
- [ ] Display partial transcripts while the user is speaking.
- [ ] Display the interpreted command before sensitive execution.
- [ ] Add a compact command history panel.
- [ ] Add a settings window for models, audio devices, voice, hotkeys, and privacy.
- [ ] Add a skill manager for viewing, enabling, disabling, and configuring skills.
- [ ] Add a permissions manager organized by user, skill, capability, and risk.
- [ ] Add a local audit-log viewer with filtering and deletion controls.
- [ ] Add first-run setup and hardware capability detection.
- [ ] Add graceful offline model installation and model-location selection.
- [ ] Ensure the UI remains responsive while models load or commands run.
- [ ] Add accessible keyboard navigation, readable status text, and configurable visual notifications.

## Audio Capture

- [ ] Enumerate and select Windows input and output devices.
- [ ] Capture mono PCM audio at the rate required by the STT provider.
- [ ] Add input-level metering and microphone testing.
- [ ] Add buffering that avoids clipping the first words of an utterance.
- [ ] Add voice activity detection and reliable end-of-utterance detection.
- [ ] Add configurable silence, maximum utterance, and cancellation timeouts.
- [ ] Add optional noise suppression and automatic gain control.
- [ ] Add echo mitigation for speech playback through speakers.
- [ ] Stop TTS immediately when the user presses push-to-talk or cancel.
- [ ] Avoid retaining raw audio unless the user explicitly enables diagnostic recording.
- [ ] Add wake-word support as an optional, independently disableable component.
- [ ] Show a persistent indication whenever passive listening is enabled.

## Speech-to-Text

- [ ] Benchmark Whisper model sizes on the GTX 1660 Super and 16 GB system memory.
- [ ] Choose the smallest model that meets accuracy requirements at near-real-time speed.
- [ ] Support CUDA inference with automatic CPU fallback.
- [ ] Load models lazily and expose model-loading progress.
- [ ] Support partial and final transcript events.
- [ ] Preserve the raw transcript before any correction.
- [ ] Return confidence indicators and language detection where supported.
- [ ] Add custom vocabulary and aliases for applications, contacts, rooms, and skill names.
- [ ] Normalize spoken numbers, times, dates, percentages, and common punctuation.
- [ ] Detect low-confidence words in sensitive arguments.
- [ ] Require clarification for uncertain recipients, paths, amounts, devices, or destructive targets.
- [ ] Add reproducible STT benchmarks using recorded test phrases and controlled noise samples.

## Intent Recognition and Routing

- [ ] Implement deterministic matching for cancel, stop, mute, confirm, deny, and help.
- [ ] Implement rule-based parsing for frequent low-ambiguity commands.
- [ ] Add a lightweight intent classifier for routing requests to relevant skill groups.
- [ ] Use the local LLM only for ambiguous, conversational, or multi-step requests.
- [ ] Require the LLM to emit schema-constrained structured output.
- [ ] Reject tool calls that fail schema validation.
- [ ] Never execute raw model-generated shell commands.
- [ ] Retrieve only relevant skill descriptions for each LLM request.
- [ ] Add confidence thresholds for direct execution, confirmation, clarification, and rejection.
- [ ] Support compound requests as an ordered execution plan.
- [ ] Detect dependencies between steps and stop safely after failures.
- [ ] Distinguish questions from requests to perform actions.
- [ ] Add recent-session context without granting implicit long-term memory.
- [ ] Add explicit commands to forget, correct, or save user-provided information.

## Local Language Model Runtime

- [ ] Benchmark candidate quantized models for latency, RAM/VRAM use, tool-call accuracy, and instruction following.
- [ ] Make the model runtime configurable without changing orchestration code.
- [ ] Use short prompts and limited context to fit the available hardware.
- [ ] Cache stable system prompts and skill metadata where supported.
- [ ] Enforce output schemas outside the model.
- [ ] Set strict generation timeouts and token limits.
- [ ] Add cancellation for in-progress generation.
- [ ] Prevent model text from bypassing the policy engine.
- [ ] Add a non-LLM fallback for core commands when the model is unavailable.
- [ ] Keep model requests and outputs local and redact secrets from prompts.
- [ ] Record performance metrics without retaining private content by default.

## Skill Framework

- [ ] Define a versioned Python skill protocol.
- [ ] Require every skill to provide a manifest and typed input/output schemas.
- [ ] Require stable skill IDs independent of display names.
- [ ] Support `validate`, `preview`, `execute`, `cancel`, `health`, and optional `rollback` operations.
- [ ] Support synchronous, asynchronous, and long-running skills.
- [ ] Provide progress events for long-running operations.
- [ ] Isolate skill failures from the desktop application process.
- [ ] Decide whether untrusted skills run in subprocesses, restricted workers, or containers.
- [ ] Apply execution timeouts and resource limits.
- [ ] Track skill versions and compatibility with the core API.
- [ ] Detect duplicate IDs and invalid manifests before loading a skill.
- [ ] Support install, update, enable, disable, uninstall, and health-check operations.
- [ ] Require permission review when an update adds capabilities.
- [ ] Provide a Python SDK, template skill, examples, and validation CLI.
- [ ] Consider an MCP-compatible adapter for future interoperability without making MCP the internal security boundary.

## Permissions and Safety

- [ ] Define permission scopes such as filesystem read/write, process launch, input control, network, messaging, and device control.
- [ ] Assign every skill and action a risk level.
- [ ] Default newly installed skills to no granted permissions.
- [ ] Show why a permission is needed and what data or system area it exposes.
- [ ] Support one-time, session-only, always, and deny grants.
- [ ] Allow grants to be limited to folders, applications, contacts, or devices.
- [ ] Require confirmation immediately before destructive or irreversible actions.
- [ ] Display exact targets for deletions, messages, purchases, locks, and security changes.
- [ ] Require stronger confirmation for critical actions, such as typed PIN or Windows authentication.
- [ ] Ensure spoken “yes” cannot approve an unrelated or expired action.
- [ ] Bind confirmations to a single immutable action and expiration time.
- [ ] Prevent skills from requesting broader permissions during execution without returning to the user.
- [ ] Add dry-run and preview support for filesystem and bulk operations.
- [ ] Add rollback where technically reliable, while never promising rollback when it is not guaranteed.
- [ ] Log decisions, grants, denials, executions, and outcomes.
- [ ] Provide an emergency stop that cancels speech, plans, and active skill jobs.

## Windows Skills

- [ ] Open, focus, minimize, maximize, and close approved applications.
- [ ] Launch applications using registered paths rather than arbitrary model-generated commands.
- [ ] Control system volume, mute state, and selected media functions.
- [ ] Read and change approved Windows settings through supported APIs.
- [ ] Create notifications and reminders.
- [ ] Search approved folders and open selected files.
- [ ] Copy, move, rename, and delete files only through scoped filesystem permissions.
- [ ] Use the Recycle Bin for user-file deletion where possible.
- [ ] Provide clipboard read and write as separate permissions.
- [ ] Provide keyboard and mouse automation as high-risk, visible capabilities.
- [ ] Restrict UI automation to explicitly approved applications.
- [ ] Detect when the active window contains sensitive fields before typing.
- [ ] Add process inspection and termination with explicit target confirmation.
- [ ] Avoid exposing an unrestricted shell skill in normal installations.
- [ ] If advanced scripting is added, place it behind expert mode, strong warnings, and strict sandboxing.

## Initial General-Purpose Skills

- [ ] Timer, alarm, reminder, and local task-list skills.
- [ ] Local application launcher.
- [ ] Media playback control.
- [ ] Calculator and unit conversion.
- [ ] Local file search and safe file operations.
- [ ] Clipboard operations.
- [ ] Local notes and searchable knowledge entries.
- [ ] Date, time, and system-status queries.
- [ ] Configurable macros composed only from already permitted skills.
- [ ] Help skill that explains available commands and required permissions.
- [ ] Diagnostic skill that reports model, microphone, and skill health without exposing secrets.

## Execution Engine

- [ ] Represent multi-step requests as explicit plans with typed steps.
- [ ] Validate and authorize every step independently.
- [ ] Show a full-plan preview when any step is sensitive.
- [ ] Execute independent safe steps concurrently where appropriate.
- [ ] Execute dependent steps in order.
- [ ] Add per-step and whole-plan timeouts.
- [ ] Propagate cancellation to models, audio, subprocesses, and integrations.
- [ ] Add idempotency keys to avoid duplicate actions after retries.
- [ ] Retry only operations marked safe to retry.
- [ ] Normalize errors into user-readable and machine-readable forms.
- [ ] Preserve partial results and explain exactly where a plan stopped.
- [ ] Avoid continuing after a failed prerequisite.
- [ ] Track running jobs and expose cancel controls in the UI.

## Response Generation

- [ ] Generate concise response text from structured results, not invented success claims.
- [ ] Confirm completion only after the skill returns success.
- [ ] Distinguish completed, partially completed, failed, denied, cancelled, and timed-out outcomes.
- [ ] Offer short, normal, and detailed response modes.
- [ ] Allow individual skills to recommend speech and display responses.
- [ ] Keep sensitive content off-screen or out of speech according to user settings.
- [ ] Avoid reading passwords, tokens, private messages, or destructive command details aloud by default.
- [ ] Support follow-up clarification within a bounded session.

## Text-to-Speech

- [ ] Make speech responses globally optional.
- [ ] Add per-context modes: always speak, speak when invoked by voice, or never speak.
- [ ] Select and test a fully local TTS provider.
- [ ] Support configurable voice, speed, volume, and audio output device.
- [ ] Start playback quickly, with sentence streaming if supported.
- [ ] Support immediate interruption and cancellation.
- [ ] Prevent the assistant from transcribing its own speech where practical.
- [ ] Cache common short phrases such as confirmations and errors.
- [ ] Fall back to text when TTS is unavailable.
- [ ] Add pronunciation aliases for names, applications, and devices.

## Storage, Privacy, and Audit

- [ ] Store configuration and history locally.
- [ ] Encrypt secrets using Windows-provided user-bound encryption.
- [ ] Separate conversational history from security audit records.
- [ ] Make transcript history opt-in or configurable by retention period.
- [ ] Do not store raw audio by default.
- [ ] Add controls to clear transcripts, memory, model cache, and audit data separately.
- [ ] Record which skill acted, which permissions were used, and the final result.
- [ ] Redact secrets and sensitive arguments from normal logs.
- [ ] Protect the database against concurrent access and interrupted writes.
- [ ] Add export and backup for non-secret configuration.
- [ ] Add database migration, recovery, and corruption-handling procedures.
- [ ] Verify through tests that offline mode makes no outbound network requests.

## Offline Operation

- [ ] Ensure all required runtime components can be installed without cloud accounts.
- [ ] Bundle or provide an offline installation path for model files.
- [ ] Verify startup and core commands with the network adapter disabled.
- [ ] Prevent skills from using the network unless they declare and receive network permission.
- [ ] Add a global offline lock that blocks all outbound network access by assistant components.
- [ ] Clearly label optional integrations that cannot work offline.
- [ ] Avoid cloud telemetry and crash reporting by default.
- [ ] Provide local diagnostics that users can export manually.
- [ ] Verify installers and model packages with checksums or signatures.

## Performance and Hardware Targets

- [ ] Define latency budgets for audio activation, partial transcript, final transcript, intent, execution, and first TTS audio.
- [ ] Target visible listening feedback in under 100 ms.
- [ ] Target partial transcription while speech is in progress where supported.
- [ ] Target simple deterministic commands completing without an LLM round trip.
- [ ] Keep peak memory use within a safe margin below 16 GB.
- [ ] Avoid loading large STT, LLM, and TTS models onto the GTX 1660 Super simultaneously if VRAM pressure harms latency.
- [ ] Support configurable model unloading and CPU/GPU placement.
- [ ] Warm commonly used models after startup or first invocation according to user preference.
- [ ] Degrade gracefully to smaller models under resource pressure.
- [ ] Add a benchmark screen showing real-time factor, token speed, memory, and command latency.
- [ ] Test sustained use for memory leaks and audio-device failures.

## External Device Expansion

- [ ] Keep device communication behind an integration interface.
- [ ] Add MQTT or another authenticated local message transport when distributed components are introduced.
- [ ] Define device identity, capability discovery, health, and command-result contracts.
- [ ] Require mutual authentication for remote devices.
- [ ] Encrypt local-network traffic carrying audio, commands, or private data.
- [ ] Add replay protection and command expiration.
- [ ] Pair devices through an explicit local approval flow.
- [ ] Support revoking a lost or compromised device.
- [ ] Treat external-device actions as permission-scoped skills.
- [ ] Add Home Assistant as an optional device integration rather than the core application platform.
- [ ] Add Wyoming-compatible adapters if room microphones or local speech satellites are needed.
- [ ] Preserve local desktop functionality when the device-control service is unavailable.

## Testing

- [ ] Unit-test schemas, routing, normalization, permissions, confirmation binding, and risk classification.
- [ ] Contract-test every model provider and skill implementation.
- [ ] Integration-test audio-to-command and command-to-response flows.
- [ ] Add recorded-audio tests for accents, noise, silence, interruptions, and low-confidence words.
- [ ] Add adversarial prompt tests attempting to bypass permissions or call unavailable skills.
- [ ] Test malformed and hallucinated tool calls.
- [ ] Test cancellation during every pipeline stage.
- [ ] Test duplicate events, retries, timeouts, crashes, and partial failures.
- [ ] Test destructive operations only against temporary sandbox directories and mock adapters.
- [ ] Verify the assistant cannot act outside granted filesystem and application scopes.
- [ ] Verify permission changes are audited.
- [ ] Verify no-network operation using firewall or network-isolated tests.
- [ ] Add end-to-end latency benchmarks for target hardware.
- [ ] Test packaged builds on a clean, non-developer Windows account.
- [ ] Add upgrade and database-migration tests.

## Observability and Diagnostics

- [ ] Add structured local logging with correlation IDs.
- [ ] Track timing for each pipeline stage.
- [ ] Track STT confidence, routing decisions, and skill outcomes.
- [ ] Provide a privacy-safe diagnostic bundle generator.
- [ ] Add model and skill health checks.
- [ ] Detect unavailable microphones, GPUs, model files, and audio outputs.
- [ ] Show actionable recovery guidance rather than raw stack traces.
- [ ] Add a developer mode with expanded traces and schema inspection.
- [ ] Keep production logs free of raw secrets and unnecessary transcript content.

## Packaging and Updates

- [ ] Produce a reproducible Windows build.
- [ ] Include required native libraries and verify CUDA compatibility.
- [ ] Support CPU-only installation when CUDA is unavailable.
- [ ] Create an installer with selectable model components.
- [ ] Avoid requiring administrator privileges for normal installation where possible.
- [ ] Sign executables and installers before public distribution.
- [ ] Define an offline/manual application update path.
- [ ] Define signed skill-package installation and verification.
- [ ] Preserve user settings, permissions, and audit history across upgrades.
- [ ] Require renewed approval when updates materially expand permissions.
- [ ] Add uninstall options for application data and downloaded models.

## Documentation

- [ ] Write a threat model covering voice spoofing, malicious skills, prompt injection, and compromised integrations.
- [ ] Document the full audio-to-action data flow.
- [ ] Document each permission and risk level in plain language.
- [ ] Document how confirmation works and what it cannot guarantee.
- [ ] Create user guides for setup, microphones, models, skills, and privacy.
- [ ] Create developer guides for writing, testing, and packaging Python skills.
- [ ] Document supported hardware and measured performance.
- [ ] Document offline guarantees and any optional network features.
- [ ] Maintain a compatibility matrix for application, skill API, Python, models, and Windows versions.

## Delivery Milestones

### Milestone 1: Push-to-Talk Prototype

- [ ] Desktop shell and global push-to-talk hotkey work.
- [ ] Local microphone audio is transcribed offline.
- [ ] Raw and final transcripts appear in the UI.
- [ ] Cancel works during capture and transcription.
- [ ] Measured latency is recorded on the target computer.

### Milestone 2: Safe Command MVP

- [ ] Deterministic intent routing works for a small command set.
- [ ] Skill manifests, validation, and results are implemented.
- [ ] Initial timer, launcher, media, and safe file-search skills work.
- [ ] Permission prompts and audit logging work.
- [ ] No raw model or transcript text can execute directly.

### Milestone 3: Local LLM Planning

- [ ] A hardware-appropriate local model is selected through benchmarks.
- [ ] Structured tool calling works for ambiguous and multi-step requests.
- [ ] Relevant-skill retrieval limits prompt size.
- [ ] Invalid and unauthorized calls are rejected.
- [ ] Clarification and compound-command flows work.

### Milestone 4: Spoken Responses

- [ ] Local TTS is integrated and configurable.
- [ ] Speech begins with acceptable latency.
- [ ] Barge-in and cancel reliably stop playback.
- [ ] Sensitive information is not spoken by default.
- [ ] Text-only fallback works.

### Milestone 5: Security and Extensibility

- [ ] Skills can be installed, disabled, updated, and removed.
- [ ] Skill isolation, timeouts, and cancellation are implemented.
- [ ] Permissions can be scoped to folders, applications, and actions.
- [ ] Sensitive commands require action-bound confirmation.
- [ ] Threat-model and adversarial security tests pass.

### Milestone 6: Packaged Desktop Product

- [ ] First-run setup detects hardware and recommends model settings.
- [ ] The packaged app runs on a clean Windows machine.
- [ ] Installer, updates, migrations, and recovery are tested.
- [ ] Core behavior works with the network disabled.
- [ ] Performance and stability targets are met during sustained use.

### Milestone 7: External Device Control

- [ ] Secure device pairing and identity are implemented.
- [ ] Local authenticated messaging connects external devices.
- [ ] Device capabilities appear as permission-controlled skills.
- [ ] Home Assistant or direct-device adapters can be installed optionally.
- [ ] The desktop assistant remains functional during integration outages.

## MVP Release Gate

- [ ] The assistant can be installed and configured by a non-developer.
- [ ] Push-to-talk, STT, routing, execution, and optional TTS work completely offline.
- [ ] Common deterministic commands feel responsive on the target hardware.
- [ ] The application never performs a sensitive action without the required permission and confirmation.
- [ ] Every executed action has a traceable result in the local audit log.
- [ ] Skills cannot bypass schema validation or the policy engine.
- [ ] Cancellation works from every user-visible pipeline state.
- [ ] Failures produce clear messages without claiming actions succeeded.
- [ ] Automated tests cover permissions, destructive operations, tool-call validation, and offline behavior.
- [ ] A clean Windows installation passes the end-to-end acceptance suite.
