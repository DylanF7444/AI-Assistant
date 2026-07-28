# Local Assistant Interactive Acceptance Run

Run date: July 11, 2026  
Workflow reference: `docs/ACCEPTANCE_WORKFLOWS.md`

## Environment

- Windows desktop session controlled through the real packaged application UI.
- Current build: `dist/LocalAssistant.exe`, copied to a uniquely named temporary
  executable because Windows application discovery otherwise selected the older
  installed build with the same executable identity.
- Current build opened successfully as **Local Assistant**.
- Older installed build at
  `C:\Users\Dylan nonadmin\AppData\Local\Programs\LocalAssistant\LocalAssistant.exe`
  failed during startup with: `Database was created by a newer application
  version`.
- These checks use typed requests. Physical microphone and speaker acceptance
  remains a separate hardware run.

## Isolated File Fixtures

Fixtures were created only beneath:

- `C:\Users\Dylan nonadmin\Documents\LocalAssistantAcceptance`
- `C:\Users\Dylan nonadmin\Downloads\LocalAssistantAcceptance`

The set includes two deliberately ambiguous resume files, Project Atlas report
and note files, an acceptance budget CSV, empty destination directories, and
download categorization fixtures. No personal files are intended as test
targets.

## Result Legend

- **PASS:** observed behavior matches the workflow.
- **FAIL:** observed behavior contradicts the workflow.
- **PARTIAL:** useful behavior occurred, but an acceptance requirement was not
  met or could not be verified.
- **BLOCKED:** the environment or a required user-only confirmation prevented
  completion.
- **SKIPPED:** deliberately not executed because it would disrupt the active
  Windows session or affect non-fixture state.

## Initial Request Results

| ID | Workflow | Request | Result | Observed behavior |
| --- | --- | --- | --- | --- |
| START-01 | Packaged launch | Open current packaged UI | PASS | Main command window opened and showed Ready state. |
| START-02 | Packaged launch | Open previously installed build | FAIL | Startup exception reported a database created by a newer application version. |
| CORE-01 | Deterministic core | `Calculate 17 times 19.` | PASS | Returned `The result is 323.` and showed calculator execution in Activity. |
| CORE-02 | Deterministic core | `What time is it?` | PASS | Returned the current local date and time. |
| CORE-03 | Deterministic core | `Convert 72 Fahrenheit to Celsius.` | FAIL | Routed to Unit Conversion but returned `skill_failed: Unsupported unit conversion`. |
| CORE-04 | Deterministic core | `Set a timer for 3 seconds.` | PASS | Timer ran for three seconds and returned `Timer finished.` |
| CORE-05 | Device status | `Show battery status.` | PASS | Returned `Battery status retrieved.` without a prompt. |
| CORE-06 | Device status | `Show CPU and memory usage.` | PASS | Returned `CPU and memory usage retrieved.` without a prompt. |
| CORE-07 | Application launch | `Open Notepad.` | PASS | Returned `Opened notepad.` and a second Notepad window became visible. |
| CORE-08 | Device control | `Set system volume to 30 percent.` | PASS | Returned `Volume set to 30%.` |
| FILE-01 | File search | `Find resume_acceptance in Documents.` | BLOCKED | Correctly showed a scoped `filesystem.read` request for `C:\Users\Dylan nonadmin\Documents`; the automation did not grant a security/privacy permission. |
| CTX-01 | File reference | `Open the second result.` with no results | PASS | Refused to guess and explained how to select a result or provide a full path. |
| PLAN-01 | Goal clarification | `Clean up my Downloads.` | PARTIAL | Asked whether to organize by type, date, or project/name, but presented it as an answer rather than a first-class clarification. |
| PLAN-02 | Clarification continuation | `By file type.` | FAIL | Remembered the goal but asked the user to select a nonexistent file-search result instead of inspecting or requesting access to Downloads. |
| PLAN-03 | Meeting preparation | `Prepare for my meeting.` | FAIL | Ended with an Assistant Error: `Clarify revision requires clarification`; Activity also showed an unrelated Windows Setting step. |
| PLAN-04 | Recent-work retrieval | `Find what I was working on yesterday.` | FAIL | Model tried to construct a web-search plan and returned an invalid step timeout of zero; the UI exposed the invalid planner payload and validation error. |
| PLAN-05 | File organization | `Organize these files by project.` | FAIL | Returned an internal-looking explanation that no selectable skill ID was available instead of asking which files or folder. |
| PLAN-06 | Note-to-task goal | `Make this note into tasks.` | FAIL | Explained how the user could operate the Tasks skill instead of asking which note or executing a supported transformation. |
| PLAN-07 | Ambiguous control | `Turn that down.` | FAIL | Discussed why the Process skill was unsuitable instead of asking what should be reduced. |
| PLAN-08 | Comparison goal | `Compare these options.` | FAIL | Incorrectly executed Web Fetch and failed because outbound network access was blocked, instead of asking which options. |
| PLAN-09 | Troubleshooting goal | `Fix this.` | FAIL | Discussed high-risk input automation rather than identifying the target or asking what `this` meant. |
| TASK-01 | Tasks | `Add a task review the Local Assistant acceptance findings.` | PASS | Tasks skill completed and the local list count increased to three. |
| TASK-02 | Tasks | `List tasks.` | PARTIAL | Returned only `3 tasks in the local list` and did not show task titles. |
| NOTE-01 | Notes | `Save a note the acceptance replacement filter is model X200.` | PASS | Notes skill completed; a subsequent X200 search found one note. |
| NOTE-02 | Notes | `Search my notes for X200.` | PARTIAL | Found one matching note but displayed only the count, not the matched note. |
| MEM-01 | Explicit memory | `Remember that acceptance theme is teal.` | PASS | Explicitly confirmed `I will remember: acceptance theme is teal`. |
| MEM-02 | Explicit memory | `What do you remember?` | PARTIAL | Returned only the count of enabled memories. |
| MEM-03 | Memory review UI | Open Memories tab | PASS | Displayed the acceptance memory with status, type, key, value, source, confidence, and edit/enable/delete controls. |
| BROWSER-01 | Browser utility | `Open a new browser tab.` | PARTIAL | Returned success but created a second Brave window rather than a tab in the existing window. |
| BROWSER-02 | Browser utility | `Refresh the page.` | FAIL | Context Navigation remained executing, eventually reported `[WinError 87] The parameter is incorrect`, and required cancellation. |
| CANCEL-01 | Cancellation | Cancel stuck Context Navigation | PASS | Escape changed the status to `Stopped by you` and added Cancelled entries to Activity. |
| DIAG-01 | Diagnostics | `Run diagnostics.` | PARTIAL | Reported one item needing attention but did not identify it in the Assistant response. |
| AUDIT-01 | Audit | Open Audit Log | PASS | Audit table visibly contained routing, authorization, skill execution, skill IDs, and decisions such as allowed/completed/failed. |
| FILE-02 | File copy | Copy the exact acceptance resume path to the exact Destination path | PASS | Returned `Copy completed`; source and destination SHA-256 hashes matched. |
| FILE-03 | File rename | Rename the copied fixture to `resume_acceptance_renamed.txt` | PASS | Returned `Rename completed`; the old destination name no longer existed. |
| FILE-04 | File move | Move the renamed fixture to the exact Moved path | PASS | Returned `Move completed`; only the final path existed and its hash still matched the source. |
| FILE-05 | File open | Open the exact moved fixture path | PASS | Returned `Open completed`; Notepad opened `resume_acceptance_renamed.txt`. |
| PERM-02 | Confirmation configuration | Inspect persisted confirmation settings | BLOCKED | This profile explicitly disables medium/high and file-operation confirmation prompts. The automation did not change those security settings, so standard confirmation UX could not be accepted in this run. |
| APP-01 | Registered applications | `List registered applications.` | PARTIAL | Listed applications and exact paths, but the output was excessively long and included uninstallers, installers, package-cache executables, and duplicate Assistant/browser aliases. |
| BROWSER-03 | Browser search | `Open Brave and search for local LLM benchmarks.` | PASS | Opened Brave to the exact encoded Brave Search URL; the visible window title confirmed the query. |
| BROWSER-04 | Preference context | `Use my usual browser.` | PASS | Used the remembered browser context and asked for the missing URL instead of inventing one. |
| BROWSER-05 | Browser context | `New tab.` | FAIL | Context Navigation again stalled and returned `[WinError 87] The parameter is incorrect`; cancellation was required. |
| VOICE-01 | Voice UI | Start microphone capture, then cancel | PASS | UI entered Listening, enabled Stop, showed microphone activity, and recorded cancellation after 0.8 seconds. No spoken recognition was attempted. |
| CANCEL-02 | Planning cancellation | Cancel `Prepare for my meeting.` during interpretation | PASS | Status changed from `Interpreting command` to `Stopped by you` after 1.2 seconds. |
| CANCEL-03 | Execution cancellation | Cancel a 30-second timer during execution | PASS | Timer stopped after 1.2 seconds and returned `The action was cancelled.` |
| MODEL-01 | Planner degradation | Continue deterministic requests after planner failures | PASS | Calculator, timer, task, note, memory, file, browser-launch, diagnostics, and device commands continued working after invalid planner responses. |

## Repair Retest Results

The following checks were repeated against
`LocalAssistantAcceptanceBuild4.exe`, an exact copy of the final rebuilt
`dist/LocalAssistant.exe` produced at 4:03:05 PM. The final installer was then
compiled successfully at 4:05:30 PM.

| ID | Workflow | Request or check | Result | Observed behavior |
| --- | --- | --- | --- | --- |
| RETEST-01 | Packaged launch | Start isolated final executable | PASS | Final packaged UI opened against the existing profile and reached Ready. |
| RETEST-02 | Temperature conversion | `Convert 68 degrees Fahrenheit to Celsius` | PASS | Returned `68 fahrenheit is 20 celsius.` and recorded Unit Conversion execution. |
| RETEST-03 | Goal clarification | `Clean up Downloads` | PASS | Asked whether to organize by file type, date, or project/name. |
| RETEST-04 | Meeting clarification | `Prepare for meeting` | PASS | Asked which meeting to prepare for, without executing an unrelated skill. |
| RETEST-05 | Comparison clarification | `Compare these options` | PASS | Asked which options to compare. |
| RETEST-06 | Troubleshooting clarification | `Fix this` | PASS | Asked what should be fixed and what `this` referred to. |
| RETEST-07 | Follow-up cancellation | `cancel` during each clarification | PASS | Returned `Cancelled the pending follow-up.` and cleared pending state. |
| RETEST-08 | Task detail | `list tasks` | PASS | Displayed task IDs, completion markers, and titles instead of only a count. |
| RETEST-09 | Note detail | `search notes for acceptance` | PASS | Displayed the matching note ID, title, and body. |
| RETEST-10 | Memory detail | `What do you remember about me?` | PASS | Displayed the enabled count plus memory IDs, types, keys, and values. |
| RETEST-11 | Registered apps | `list registered applications` | PASS | Displayed capped, deduplicated aliases without executable paths; unsafe installer/update paths are filtered by code and regression tests. |
| RETEST-12 | Browser missing | `open a new tab` with no browser open | PASS | Failed clearly with `No focused or visible browser window was found`. |
| RETEST-13 | Browser launch | `open Brave` | PASS | Opened the registered Brave browser with one visible New Tab. |
| RETEST-14 | Browser shortcut | `open a new tab` with Brave visible | PASS | Assistant reported completion and Brave tab count increased from one to two. |
| RETEST-15 | Browser shortcut | `close tab` | PASS | Assistant reported completion and Brave tab count returned from two to one. |
| RETEST-16 | Regression suite | `python -m pytest -q` | PASS | 503 tests passed. |
| RETEST-17 | Supplemental suites | Learning and UI tests | PASS | 29 learning tests and 6 UI tests passed. |
| RETEST-18 | Executable build | PyInstaller build and cached verification | PASS | Final build completed; a second verification invocation exited 0. |
| RETEST-19 | Installer build | Inno Setup compile | PASS | Compiler exited 0 and produced `LocalAssistant-0.1.0-Setup.exe`. |

## Deferred or Skipped Cases

| ID | Workflow | Result | Reason |
| --- | --- | --- | --- |
| VOICE-02 | Real spoken command streak and transcription accuracy | BLOCKED | Requires a person speaking a controlled corpus into the physical microphone. |
| VOICE-03 | Cancel during transcription and speech playback | BLOCKED | No controlled spoken input was supplied and Piper is not configured in this profile. |
| FILE-06 | Recycle/delete fixture | BLOCKED | Local deletion through UI requires a fresh action-time user confirmation. |
| PERM-03 | Once/session/always grant lifecycle | BLOCKED | The automation is not permitted to choose security/privacy grants or change permission settings. |
| SAFE-01 | Lock, restart, shutdown, Wi-Fi disable | SKIPPED | Would disrupt the active Windows test session or connectivity. |
| MODEL-02 | Stop Ollama and retest model-unavailable behavior | SKIPPED | The run observed invalid planner output, but did not stop an unrelated running model service without a separate explicit action. |
| PACKAGE-01 | Upgrade and uninstall | SKIPPED | These are destructive installation-state workflows and were not required to establish the interactive request findings. |

## Main Findings

1. The deterministic local core and final repaired package are credible for the
   tested calculator, conversion, timer, device, app-launch, exact-path file,
   productivity, memory, browser, cancellation, and audit workflows.
2. Canonical broad goals now take a deterministic safe-clarification path, so
   the tested underspecified requests no longer depend on small-model planning
   quality or expose invalid model payloads.
3. Browser Context Navigation now uses a Win32 `INPUT` structure matching the
   native ABI. Live New Tab and Close Tab trials changed Brave's tab count as
   expected without WinError 87.
4. Task, note, and memory responses now show useful record details, and the
   registered-app response hides paths, deduplicates aliases, caps output, and
   filters unsafe executable names.
5. The older installed executable still cannot open the schema used by the
   current profile. The final isolated build opens it correctly; installed-copy
   upgrade behavior remains a separate installer workflow.
6. Current persisted settings disable the confirmation prompts needed to accept
   the standard high-risk confirmation workflow, and automation did not alter
   those security settings.

## Safety Notes

- Requests that lock, restart, shut down, disable networking, or alter unrelated
  user applications are excluded from unattended execution.
- Local deletion requires an action-time user confirmation even when the target
  is an acceptance fixture. Delete workflows may be tested through preview and
  Assistant confirmation generation without completing deletion.

## In-App Media Implementation Acceptance — July 27, 2026

This section records the automated and packaged checks for the in-app image,
audio, and video presentation implementation. It does not replace the physical
hardware and codec matrix in `docs/MEDIA_VIEWER.md`.

| ID | Workflow | Result | Evidence |
| --- | --- | --- | --- |
| MEDIA-01 | Contracts and propagation | PASS | Strict media source/directive/action contracts and response propagation tests passed. |
| MEDIA-02 | Local resolution and authorization | PASS | Canonical resolution, bounded signature probing, exact permission, changed-file, and session tests passed. |
| MEDIA-03 | Routing and conversation references | PASS | Show/pull-up/play/control, selected-result, negative-routing, and ambiguity tests passed. |
| MEDIA-04 | Image viewer | PASS | Offscreen Qt image load, error, zoom, pan, rotate, fullscreen, compact/full, stale-directive, and cleanup tests passed. |
| MEDIA-05 | Audio/video viewer | PASS | Playback state, seek, skip, volume, mute, rate, decoder-error, matching-session, and runtime-format tests passed. |
| MEDIA-06 | Audio focus and lifecycle | PASS | Viewer/TTS/capture priority, resume safety, Emergency Stop, replacement, and shutdown tests passed. |
| MEDIA-07 | Remote and privacy safety | PASS | Offline, SSRF, redirects, MIME/signature, size, timeout, cancellation, cache retention/clear, and URL-redaction tests passed offline. |
| MEDIA-08 | Full regression | PASS | The expanded 791-test production suite and 29 learning tests passed after detached results-window integration. |
| MEDIA-09 | Static checks | PASS | Ruff format and lint checks passed across 259 Python files; `git diff --check` reported no whitespace errors. |
| MEDIA-10 | Windows executable | PASS | PyInstaller produced `dist/LocalAssistant.exe`; packaged runtime inspection found Qt Multimedia, FFmpeg and Windows media backends, and JPEG/GIF/WebP decoders. |
| MEDIA-11 | Packaged isolated startup | PASS | The executable created a fresh isolated database and completed a text command with exit code 0. |
| MEDIA-12 | Inno Setup installer | BLOCKED | Inno Setup is not installed in this environment; the existing installer was not presented as a current build. |
| MEDIA-13 | Physical packaged media matrix | BLOCKED | Requires a person to assess visible frames, audible output, microphone interaction, device routing, and target-hardware codecs. |

The Windows app-control launcher was also tried against the current executable.
It cannot pass the required `--data-dir` isolation argument, and the default
profile did not expose a targetable window. The run did not change the user's
real permission or privacy settings to bypass that isolation boundary.

## Notification Inbox and Results Acceptance — July 28, 2026

| ID | Workflow | Result | Evidence |
| --- | --- | --- | --- |
| NOTIFY-01 | Persistence and migration | PASS | Schema 10 migration, restart persistence, bounded retention, unread/read/dismissed state, and URL credential/query redaction tests passed. |
| NOTIFY-02 | Inbox and foreground UI | PASS | Offscreen Qt tests covered unread badges, Inbox rows/details/actions, recent-item drawer, foreground banner activation, and exact notification selection. |
| NOTIFY-03 | Run and report deep links | PASS | Completed run notifications opened the separate report view; approvals and non-result runs retained exact Automations row selection. |
| NOTIFY-04 | Detached media results | PASS | Image presentation opened the independent results window while the Assistant remained compact; controls, cards, lifecycle, and saved geometry tests passed. |
| NOTIFY-05 | Delivery policy | PASS | Settings validation covered independent in-app/system delivery, overnight quiet hours, durable recording, and error interruption policy. |
| NOTIFY-06 | Full automated regression | PASS | `python -m pytest` passed 791 tests in 146.55 seconds; `python -m pytest learning_tests -q` passed 29 tests. |
| NOTIFY-07 | Packaged Windows notification | PENDING | Installer shortcut AppUserModelID and runtime identity are configured; visible Windows notification-center delivery and activation still require a packaged interactive Windows acceptance run. |
