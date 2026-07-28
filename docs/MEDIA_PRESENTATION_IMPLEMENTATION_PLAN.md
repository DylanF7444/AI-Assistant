# In-App Media Presentation Implementation Plan

## Objective

Implement a permission-gated, foreground-only media presentation pipeline that
can display local or explicitly downloaded images, audio, and video inside the
desktop application. Typed and spoken requests such as `show the second photo`,
`play meeting.mp3`, `pause this video`, and `close the viewer` must use the
normal router, schema validation, policy, permission, execution, response, and
audit boundaries.

The existing `windows.media` skill remains responsible for global Windows media
keys. In-app viewer commands use separate `media.present` and
`media.viewer_control` skills.

## Non-goals

- Generic image or video web search.
- Embedded website or YouTube playback.
- DRM, HLS, DASH, or authenticated streaming services.
- Media editing, conversion, trimming, generation, OCR, or transcription.
- Implicit full-drive searches.

## Architecture

1. The deterministic router or local planner produces a registered media skill
   intent.
2. The media reference resolver turns selected-result, exact-path, named-folder,
   or explicit HTTPS input into a bounded source.
3. The skill validates its input and requests exact `filesystem.read` or
   `network` permission.
4. File policy and execution reauthorization run through `ExecutionEngine`.
5. A successful skill result carries an immutable `MediaDirective`.
6. `response_from_result` propagates the directive into the desktop response.
7. `DesktopController` gives the directive to `MediaViewer`.
8. The viewer renders with an image backend or Qt Multimedia and reports its
   applied/failed/closed state to `MediaSessionRegistry`.

Skill completion means the source was authorized and prepared. Viewer load
success or failure is recorded separately so an asynchronous decoder failure is
never reported as a successful display.

## Sequential Feature Gates

### Gate 1: Typed presentation contracts

- Add strict media kind, source, action, directive, and state contracts.
- Add optional media directives to `SkillResult` and `Response`.
- Propagate directives through `response_from_result`.
- Preserve directives through plan aggregation where applicable.
- Add immutability, versioning, action-field, and round-trip tests.

Complete when all new contract tests and the existing contract/response tests
pass.

### Gate 2: Local preparation and skill execution

- Add a media signature/type probe with bounded metadata reads.
- Add canonical local-path resolution and fingerprints.
- Add an in-memory `MediaSessionRegistry`.
- Implement `media.present`.
- Implement `media.viewer_control`.
- Require exact local read scopes and foreground-only execution.
- Register the services and skills at bootstrap.
- Add skill, permission, policy, malformed-file, and session tests.

Complete when a prepared local PNG, WAV, and MP4-like fixture produces a typed
directive without reading full content, and invalid sources fail safely.

### Gate 3: Routing and conversation resolution

- Add guarded `show`, `pull up`, `view`, and explicit local `play` routes.
- Add viewer play/pause/seek/volume/rate/fullscreen/close routes.
- Keep generic explanations, application launches, web searches, and global
  media commands on their existing routes.
- Generalize selected-file resolution for `media.present`.
- Track and clear the active media reference in conversation state.
- Add planner examples and deterministic negative tests.

Complete when supported requests route without the planner, ambiguous requests
clarify, and existing router regression tests remain green.

### Gate 4: Image viewer

- Create a standalone desktop media viewer module instead of growing
  `ui/desktop/app.py`.
- Add text, loading, image, audio, video, and error stacked states.
- Render images with bounded `QImageReader` decoding.
- Support fit, 100%, zoom, pan, rotate, fullscreen, close, and accessible
  keyboard controls.
- Add a compact now-viewing card and expand-to-full behavior.
- Reject stale directives and changed files.

Complete when offscreen Qt tests cover success, failure, controls, compact/full
transitions, and cleanup.

### Gate 5: Audio and video viewer

- Add a testable media-player backend protocol.
- Implement the Qt `QMediaPlayer`, `QAudioOutput`, and `QVideoWidget` backend.
- Add play/pause/stop, seek, skip, mute, volume, playback speed, timestamps,
  fullscreen, and decoder errors.
- Apply voice/typed viewer-control directives only to the matching session.
- Detect runtime-supported formats.

Complete when fake-backend UI tests cover every state transition and a local Qt
smoke test can load supported audio/video media.

### Gate 6: Playback, TTS, microphone, and cancellation coordination

- Replace the single capture playback Boolean with a source-aware coordinator.
- Pause viewer media for explicit push-to-talk and resume only when safe.
- Give TTS priority over viewer playback.
- Stop viewer playback on Emergency Stop and application shutdown.
- Suspend passive activation during speaker playback unless an explicitly
  tested headset mode is enabled.
- Add overlap, cancellation, and resume tests.

Complete when no playback source can incorrectly re-enable capture while
another source remains active and all cancellation paths release resources.

### Gate 7: Guarded remote media and privacy

- Accept only explicit public HTTPS media URLs while offline lock is disabled.
- Reject credentials, private/local destinations, unsafe redirects, non-media
  content, and oversized downloads.
- Download to a bounded application cache before giving content to Qt.
- Make downloads cancellable and report progress.
- Add cache retention and privacy clearing.
- Redact query strings, file contents, thumbnails, and frames from logs.

Complete when SSRF, redirect, offline, size, MIME, cancellation, and cache-clear
tests pass without external network dependencies.

### Gate 8: Packaging, diagnostics, documentation, and release acceptance

- Package Qt Multimedia and image/codec plugins.
- Report multimedia availability, output devices, formats, cache size, and
  sanitized decoder failures in Diagnostics.
- Update architecture, permissions, threat model, features, user guide,
  checklists, and acceptance workflows.
- Run regression, learning, UI, build, and packaged Windows acceptance checks.

Complete when the full automated suite passes and the packaged application has
been manually exercised with the documented image/audio/video matrix.

## Security Requirements

- Resolve and authorize canonical exact targets.
- Recheck path, type, size, and fingerprint immediately before UI load.
- Never load a directory, missing file, wildcard, unresolved placeholder, or
  unsupported reparse target.
- Apply file whitelist/blacklist independently from grants.
- Autoplay only for an explicit play request.
- Never pass an unvalidated remote URL directly to Qt.
- Never persist the last media path or playback position by default.
- Never log decoded content, thumbnails, URL credentials, or query strings.
- Opening an unsupported file externally must re-enter
  `windows.file_operation` and its `process.launch` permission.

## Verification Commands

```powershell
python -m pytest tests/test_media_contracts.py tests/test_response.py -q
python -m pytest tests/test_media_probe.py tests/test_media_skills.py -q
python -m pytest tests/test_media_routing.py tests/test_media_orchestration.py -q
python -m pytest -m ui tests/ui/test_media_viewer.py -q
python -m pytest
python -m pytest learning_tests -v
python -m pytest -m ui tests/ui -q
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
```

## Implementation Status — July 27, 2026

All eight engineering gates are implemented and automated acceptance is green.

| Gate | Status | Release evidence |
| --- | --- | --- |
| Typed contracts | Complete | Strict contract, response propagation, validation, and round-trip tests pass. |
| Local preparation | Complete | Signature probing, canonical resolution, exact permissions, sessions, and both skills pass focused tests. |
| Routing and references | Complete | Deterministic positive, negative, selected-result, ambiguity, and control tests pass. |
| Image viewer | Complete | Offscreen Qt load/error/fit/zoom/pan/rotate/fullscreen/compact lifecycle tests pass. |
| Audio and video | Complete | Fake-backend control/state/error tests and real Qt format discovery pass. |
| Audio coordination | Complete | TTS, capture, viewer, cancellation, Emergency Stop, and shutdown focus tests pass. |
| Remote and privacy | Complete | Offline, SSRF, redirect, MIME, signature, size, timeout, cancel, cache, and redaction tests pass. |
| Packaging and diagnostics | Complete | PyInstaller build, packaged runtime inspection, Diagnostics tests, and isolated packaged startup pass. |

Final automated results:

- 743 production regression tests passed.
- 29 guided learning tests passed.
- 33 UI tests passed; 4 unrelated marker selections were deselected.
- Ruff format and lint checks passed across 259 Python files.
- `dist/LocalAssistant.exe` built successfully and
  `scripts/verify_packaged_media.py` confirmed Qt Multimedia, multimedia
  backends, and required image decoder plugins in the archive.
- The packaged executable initialized a fresh isolated data store and exited
  successfully through its text-command entry point.

Inno Setup is not installed on this machine, so the installer was not
recompiled. The source, executable, and automated implementation are complete;
release sign-off still requires the physical speaker/microphone and packaged
codec matrix below on intended Windows hardware.

## Final Acceptance

The implementation is complete when an approved local image, audio file, or
video can be selected and shown inside the application; every viewer control
works from mouse, keyboard, typed text, and supported voice phrasing; ambiguous
or blocked sources do not load; unsupported media fails with recovery guidance;
autoplay is explicit; offline lock blocks remote preparation; cancellation and
shutdown stop playback; and the packaged Windows build passes the documented
media matrix.
