# Local Assistant Acceptance Workflow Roadmap

Updated August 6, 2026.

This document turns the product workflow roadmap into a repeatable acceptance
reference. The emphasis is a usable, trustworthy local Windows product rather
than feature breadth.

## Test Lanes

Run each important workflow through four lanes:

1. **Deterministic:** a precise command executes without the LLM.
2. **Interpretive:** a natural or goal-oriented request is understood and safely
   planned.
3. **Ambiguous:** missing information causes useful clarification rather than a
   guess.
4. **Degraded:** model, device, permission, target, or skill failure produces an
   actionable explanation.

Every workflow should also verify cancellation, permission handling, visible
progress, result reporting, and audit history.

## 1. Voice Command Acceptance

### Goal

Prove the complete microphone-to-result loop works reliably with real speech.

### Core workflow

1. User presses push-to-talk.
2. Assistant shows the listening state.
3. User speaks a deterministic command.
4. The transcript appears.
5. The Assistant routes the command without the LLM.
6. Permission or confirmation appears when needed.
7. The action executes.
8. The result is displayed and, when configured, spoken.
9. The audit log records the decision and outcome.

### Direct-command examples

- "Open Notepad."
- "Open File Explorer."
- "Set the volume to 30 percent."
- "Mute the audio."
- "Turn off Wi-Fi."
- "Show battery status."
- "Show CPU and memory usage."
- "Set a timer for three minutes."
- "Remind me in ten minutes to check the oven."
- "Calculate 17 times 19."
- "Convert 72 Fahrenheit to Celsius."
- "Find budget in Documents."
- "Open the second result."
- "Copy that file to Desktop."
- "Open a new browser tab."
- "Go back."
- "Refresh the page."
- "Focus Notepad."
- "Lock the device."

### Natural-language variation

- "Could you turn the sound down to thirty percent?"
- "I need a blank Notepad window."
- "How much battery do I have left?"
- "Look in my documents for something named resume."
- "Take me back one page in the browser."
- "Start a five-minute tea timer."

The transcript and request breakdown should show whether the deterministic or
planner path was chosen.

### Ambiguity and correction

- "Turn that down" asks what should be reduced unless context resolves it
  safely.
- "Open the report" asks which report when several candidates exist.
- "Delete the second one—no, the third one" updates the target and invalidates
  prior confirmation.
- "Set volume to thirteen—actually thirty percent" executes only the corrected
  value.
- A low-confidence filename, process, device, or destructive verb requires
  clarification.

### Voice-driven safety prompts

- Grant permission once.
- Grant permission for the session.
- Deny permission.
- Approve and reject immediate confirmation.
- Speak the stronger token for a critical action.
- Cancel while a permission or confirmation prompt is visible.

### Environmental cases

- Quiet room, fan noise, music, and another person speaking.
- Microphone disconnected before and during capture.
- Default input or output changes mid-session.
- Assistant speech reaches the microphone.
- User interrupts speech with another command.
- Long silence, partial utterance, and speech timeout.

### Done when

- A mixed run of at least 50 spoken commands completes without restarting.
- At least 20 commands are deterministic, 10 require context or planning, 10
  require permission or confirmation, and 10 exercise correction,
  cancellation, or failure.
- No sensitive action executes from a low-confidence target.
- Every failure identifies the failed stage: listening, transcription,
  interpretation, permission, confirmation, execution, or speech.
- Cancel works from every visible pipeline state.

## 2. Safe File Operation

### Goal

Make filesystem actions predictable, inspectable, and recoverable.

### Search and selection

- "Find resume in Documents."
- "Show me more results."
- "Open the newest one."
- "Select the PDF."
- "Open the report from the tax folder."
- "Copy the second result to Desktop."
- "Rename that file to Final Resume."
- "Move it to Documents."

Test duplicate names in different directories, spaces and punctuation, hidden
files, long paths, unavailable drives, and results that disappear after search.

### Planner-assisted file goals

- "Clean up my Downloads."
- "Organize these files by project."
- "Put the images in one folder and the PDFs in another."
- "Find what I was working on yesterday."
- "Gather the files related to Project Atlas."

These begin with inspection and clarification. The Assistant produces a preview
containing exact paths and operations before changing anything.

### Collision and change handling

- Destination already contains a file with the same name.
- Source changes between preview and confirmation.
- Destination becomes unavailable.
- Another application locks a file.
- One operation succeeds and a later operation fails.
- User changes the destination after preview.

The Assistant stops or replans safely. It never silently overwrites, invents a
new target, or continues with stale confirmation.

### File-policy boundaries

- Search an approved folder.
- Attempt to move a result outside approved write scope.
- Add a blacklist entry beneath an otherwise allowed folder.
- Revoke filesystem permission after preview but before execution.
- Try path traversal, environment-variable tricks, shortcuts, and junctions
  that escape the approved location.

### Recovery

- Delete sends the file to the Recycle Bin.
- Cancel before confirmation changes nothing.
- Supported rollback restores prior state after multi-step failure.
- Audit history distinguishes preview, approval, completion, failure,
  rollback, and recycle.

### Done when

- Search, open, copy, move, rename, and recycle work only on resolved, approved
  paths.
- "That file," "the second one," and "the PDF" resolve only when unambiguous.
- Batch organization always has a dry-run preview.
- Changed paths or arguments invalidate confirmation.
- Partial failure reports exactly what changed and what did not.
- No deletion path silently hard-deletes.

## 2A. In-App Media Presentation

### Goal

Present approved media in the Assistant without external application launch,
stale references, unbounded decoding, or direct network playback.

### Local selection and images

- Show exact PNG, JPEG, GIF, BMP, and WebP files.
- Search one approved Pictures subfolder for a unique name.
- Return numbered choices for duplicate names and show the selected result.
- Exercise fit, 100%, mouse-wheel and button zoom, pan, both rotations,
  fullscreen/Escape, replacement, close, and compact/full transitions.
- Change a file after authorization and verify the viewer rejects it.
- Try malformed signatures, extension/type mismatches, and an over-limit image.

### Audio and video

- Play a WAV, MP3 or other runtime-listed compressed audio file.
- Play MP4 and one alternate runtime-listed video container.
- Exercise mouse, keyboard, typed, and spoken play/pause/stop, absolute and
  relative seek, skip, mute, volume, playback rate, fullscreen, and close.
- Verify a stale control directive cannot affect a replacement presentation.
- Verify unsupported codecs show recovery guidance and a sanitized diagnostic
  code without launching another application.

### Audio coordination and lifecycle

- Start push-to-talk while media plays; media pauses before capture.
- Finish an unrelated command; the same untouched media resumes.
- Say `pause it` during the interruption; it remains paused.
- Trigger TTS while viewer and passive voice mode are active; only one output
  focus aggregate suppresses capture until all speaker output stops.
- Use Emergency Stop and Quit from each playback state; output and capture
  resources are released and no pending resume fires.

### Guarded remote media

- Disable offline lock, approve the exact network target, and show a direct
  public HTTPS image/audio/video URL containing a temporary query parameter.
- Confirm the query parameter is absent from provenance, terminal output, audit,
  and diagnostics.
- Verify HTTP, URL credentials, loopback/private/link-local/reserved hosts,
  unsafe redirects, HTML/wrong MIME, mismatched bytes, empty bodies, oversize
  bodies, timeouts, and cancellation all fail without a cache artifact.
- Re-enable offline lock and verify the request stops before DNS/download.
- Exercise retention/size pruning and **Clear Downloaded Media Cache**. Confirm
  local source files remain untouched.

### Packaged matrix

- Run `scripts/build.ps1` and require `verify_packaged_media.py` to find Qt
  Multimedia modules/runtime, a media backend plugin, and image plugins.
- Record Windows version, PySide/Qt version, Diagnostics format list and output
  device, every sample filename/container/codec, and expected/actual behavior in
  `ACCEPTANCE_RUN_LOG.md`.
- Repeat the image/audio/video matrix from `MEDIA_VIEWER.md` from the packaged
  executable, not only the source environment.

### Done when

- Every supported request reaches only the matching in-app presentation.
- Local and remote permission scopes are exact and foreground-only.
- Decoder success/failure is distinct from skill preparation success.
- No stale callback or directive replaces or controls a newer session.
- Cancellation leaves no temporary download or active playback resource.
- The packaged runtime passes the documented matrix on Windows 10 and 11.

## 3. Browser Utility

### Goal

Cover lightweight daily browser control without pretending to replace Windows
Voice Access or full browser automation.

### Browser targeting

- Run a command while a browser is focused.
- Run a command while a browser is visible but not focused.
- Run with two visible browsers.
- Use "Use Brave," "use my usual browser," and generic "open the browser."
- Test a missing, unregistered, and already-open preferred browser.
- Test with no available browser.

Expected targeting order is explicitly named browser, focused browser, visible
browser, remembered preference, registered default, then clear failure.

### Navigation commands

- New tab and new window.
- Close and reopen the last tab.
- Next and previous tab.
- Back and forward.
- Refresh.
- Focus the address bar.
- Open a specific HTTPS site.
- Search for a phrase containing punctuation or URL-like text.
- Open a private window when explicitly requested.

### Contextual follow-ups

- "Open Brave and search for local LLM benchmarks."
- "Open another tab and search for Windows microphone troubleshooting."
- "Go back, then refresh."
- "Use my usual browser next time" offers to store an explicit preference.

The Assistant must not claim it can open a search result or inspect page content
unless a registered skill actually provides that capability.

### Safety boundaries

- Distinguish a public HTTPS URL from a search query.
- Reject or warn on unsupported schemes.
- Do not type into a non-browser application after focus changes.
- Do not imply that page contents were inspected when only navigation occurred.

### Done when

- Context shortcuts reliably choose the correct visible browser.
- Browser launch and browser-context control are tested separately.
- Preference memory affects browser choice only after explicit approval.
- Focus changes cause a safe stop rather than input to the wrong application.
- Unsupported page interaction is described honestly.

## 4. Permission and Confirmation

### Goal

Make safety behavior visible and consistent for deterministic commands, planned
actions, macros, and replanned steps.

### Scope examples

Test permissions scoped to one exact file, folder, registered application,
process ID, device, or network operation. Broad any-target scope is used only
when the user explicitly selects it.

The prompt shows the capability, requesting skill, reason, exact targets, grant
duration, and risk.

### Grant lifecycle

- Grant once and verify the next request asks again.
- Grant for the session and restart the application.
- Grant always, then revoke it.
- Deny once and later reconsider.
- Store an explicit denial and show how to remove it.
- Narrow an existing broad grant.

### Confirmation integrity

- Change a source path after confirmation.
- Change a destination path.
- Change a process ID.
- Change an operation from volume control to shutdown.
- Reuse an expired confirmation.
- Reuse a confirmation from another session.
- Speak an incorrect critical token.

Each case requires a new confirmation or fails safely.

### Multi-step plan example

For "find the report and move it to Desktop":

1. Read permission covers only search.
2. Search results are observed.
3. Ambiguous results trigger clarification.
4. Write permission covers the exact chosen source and destination.
5. The move receives immediate confirmation.
6. Each step appears in audit history.

A plan or macro never inherits broader authority from an earlier harmless step.

### Done when

- The same permission experience covers deterministic, planner, macro, and
  replanned actions.
- Scope comes from validated arguments, not model prose.
- Every changed argument invalidates confirmation.
- Denial explains what remains possible.
- Revocation takes effect before the next execution.
- Audit history separates permission decisions and execution results.

## 5. Notes, Tasks, and Memory

### Goal

Make the Assistant useful locally while keeping durable memory explicit,
reviewable, and deletable.

### Notes

- "Save a note that the replacement filter is model X200."
- "Search my notes for filter."
- "Delete the filter note."
- Create similar notes and verify semantic and exact-word retrieval.
- Search for a nonexistent note and receive a clear empty result.

### Tasks

- "Add a task to order the filter."
- "Show my tasks."
- "Complete the filter task."
- "Delete completed tasks" clarifies scope if batch deletion is unsupported.
- Similar task names require selection before completion or deletion.

### Explicit memory

- "Remember that I prefer Brave."
- "Use my usual browser."
- "What preferences have I asked you to remember?"
- "Change my browser preference to Firefox."
- "Forget my browser preference."
- "Delete that memory completely."

Test preference, constraint, project, workflow, identity, and correction memory
without treating ordinary conversation as durable memory.

### Planner-assisted transformations

- "Turn that note into a task."
- "Make the action items in this note into tasks."
- "Remember that I prefer short answers, then show what you saved."
- "Use my usual browser and look for the project site."
- "I no longer use Brave" offers to update or remove the preference.

The Assistant asks which note or source is meant when session context cannot
identify one.

### Sensitive memory

- Attempt to store a password, API key, access token, or private identifier.
- Require explicit acknowledgement that the value is sensitive.
- Make the item inspectable, editable, forgettable, and deletable.
- Redact it from logs and debug displays.

### Done when

- Long-term memory is created only by an explicit request.
- Memory use is visible in the request breakdown.
- Conflicting preferences trigger clarification or an explicit update.
- Forget disables retrieval immediately; delete removes the record.
- Notes, tasks, memories, and transcript history remain distinct.

## 6. Local Model Fallback

### Goal

Prove the Assistant remains useful when local planning is unavailable or
invalid.

### Failure modes

- Ollama is not installed.
- Service is stopped.
- Model is missing.
- Model times out.
- Model returns malformed structured output.
- Model selects an unregistered skill.
- Model arguments fail schema validation.
- Global offline lock is enabled.
- The model becomes unavailable during replanning.

### Expected behavior

Deterministic volume, media, battery, calculator, timer, registered app launch,
browser shortcut, direct file search, and explicit task, note, and memory
commands continue to work.

Goal-like commands such as "Clean up my Downloads," "Help me prepare for my
meeting," "Figure out why this app is slow," and "Compare these options"
explain the unavailable planner instead of failing silently.

Responses distinguish model unavailable, invalid model response, no relevant
registered skill, unsupported command, permission denial, and blocked network.

### Done when

- No deterministic command is routed through the unavailable model.
- Planner failure never produces false success.
- Malformed output is repaired only within the bounded retry policy.
- An unavailable model never weakens validation or permissions.
- The user receives a safe alternative where one exists.

## 7. Packaged Install

### Goal

Test the product as a non-administrator on a clean Windows account.

### First run

- Install without administrator rights.
- Launch without a terminal or developer directory.
- Select input and output devices.
- Test microphone level and speech recognition.
- Configure a planner model or explicitly continue without it.
- Configure Piper or choose text-only responses.
- Register one browser and one desktop application.
- Review default file-access and privacy settings.
- Run the voice acceptance workflow.

### Persistence

After application restart and Windows sign-out:

- Settings persist.
- Registered applications persist.
- Always permissions persist.
- Session permissions do not persist.
- Notes, tasks, memories, and audit history persist.
- Pending confirmations do not persist.
- Incomplete operations are reported without unsafe automatic resumption.

### Upgrade

- Upgrade over an existing installation.
- Preserve user data and permissions.
- Apply database migration.
- Detect removed or capability-expanded skills.
- Require review when an updated skill requests new permissions.
- Verify recovery after an interrupted update.

### Uninstall

Test removal of the application alone, preservation or removal of user data,
settings and history, local models, diagnostic recordings, and startup
registration.

### Done when

- A clean Windows account completes setup without source-tree paths.
- Missing models, microphones, speakers, or registered apps produce guided
  remediation.
- Upgrade preserves data without retaining stale confirmations.
- Uninstall choices match the files actually removed.
- Executable and installer signing remains a separate release gate until a real
  certificate is available.

## 8. Goal Interpretation and Clarification

### Goal

Prove that broad requests are understood before a skill is selected.

### Examples

- "Clean up my Downloads."
- "Prepare for my meeting."
- "Find what I was working on yesterday."
- "Organize these files by project."
- "Make this note into tasks."
- "Compare these options."
- "Fix this."
- "Make this easier."

The request breakdown exposes the interpreted goal, explicit request, missing
information, assumptions, constraints, relevant context, risk notes, success
criteria, confidence, and reason for clarification.

### Done when

- Broad requests do not collapse prematurely into one action.
- Clarification asks for the smallest decision needed to continue.
- The answer resumes the original goal.
- An unrelated new request abandons pending clarification.
- Cancel clears pending goal state.

## 9. Contextual Follow-Up and Reference Resolution

### Goal

Prove that short follow-ups use bounded context without becoming unsafe guesses.

### Examples

- "Find resume in Documents" followed by "Open the second one."
- "Show battery status" followed by "What about storage?"
- "Open Brave," then "New tab," then "Search for local LLM benchmarks."
- "Save this as a note" followed by "Turn it into a task."
- "Use the usual one."
- "Move that there."
- "Do the same for the PDF."

Test selected files, recent results, focused applications, visible browsers,
remembered preferences, and pending clarifications.

### Done when

- References resolve from bounded, visible session context.
- Multiple plausible referents trigger clarification.
- Old context expires or clears predictably.
- A new topic does not reuse an old target accidentally.
- Clipboard, page, screen, window-title, and file contents remain excluded unless
  explicitly permitted.

## 10. Adaptive Multi-Step Planning and Replanning

### Goal

Validate the observe-and-revise loop rather than only static plans.

### Find and act

For "Find my latest resume and copy it to Desktop":

1. Search an approved folder.
2. Observe zero, one, or several results.
3. Stop, continue, or clarify accordingly.
4. Preview the exact copy.
5. Request permission and confirmation.
6. Execute and verify.

### Device troubleshooting

For "My computer feels slow":

1. Inspect permitted performance context.
2. Identify high-resource user applications.
3. Explain findings.
4. Ask before focusing or terminating anything.
5. Stop when the success criterion is met.

### File organization

For "Organize Downloads by file type":

1. Inspect metadata.
2. Propose groups and moves.
3. Ask about collisions and exceptions.
4. Execute only the approved subset.
5. Observe failures and stop or revise.

### Adversarial cases

- Planner repeats the same action.
- A later step selects an unavailable skill.
- Required permission is denied.
- An earlier result is empty.
- Confirmation is required mid-plan.
- Cancellation occurs between steps.
- The plan reaches the five-iteration limit.

### Done when

- Every new step is independently schema-validated and permission-checked.
- Actual result data drives the next step.
- Permission or confirmation pauses the plan visibly.
- No automatic replanning occurs after cancellation or denial.
- The bounded iteration limit produces a clear stop.

## 11. Local Troubleshooting and Diagnostics

### Goal

Turn desktop, process, device, and diagnostic context into practical assistance.

### Examples

- "Why is my computer slow?"
- "Why is my battery draining?"
- "Is my microphone working?"
- "Why didn't that command run?"
- "The focused app is stuck."
- "Why can't you access this folder?"
- "Why can't you use the model?"

The Assistant inspects only supported and permitted signals, summarizes
evidence, recommends low-risk steps, and asks before window closure, process
termination, radio changes, or power actions.

### Done when

- Diagnosis distinguishes observation from inference.
- Recommendations do not claim unsupported telemetry.
- No process is terminated solely because it uses the most memory.
- Diagnostic reports redact secrets and sensitive paths.
- The user can review a privacy-safe problem summary.

## 12. Routine and Macro Creation

### Goal

Turn repeated sequences into safe local routines.

### Examples

- "Create a focus routine that opens Notepad, sets volume to 20 percent, and
  starts a 25-minute timer."
- "Create an end-of-day routine that shows my tasks and opens Documents."
- "Run my focus routine."
- "Show what the focus routine will do."
- "Change the timer in that routine to 45 minutes."
- "Delete the routine."

### Safety behavior

- Preview the complete validated sequence before saving.
- Show combined permissions and confirmations.
- Preserve per-step permission boundaries.
- Reject recursion, unregistered skills, raw shell commands, and unresolved
  placeholders.
- Stop on failed prerequisites.

### Done when

- A macro cannot gain authority unavailable to its individual steps.
- Editing a sensitive step invalidates prior confirmation.
- Every step has an audit record.
- Failed or cancelled runs identify the last completed step.
- Repeated behavior never becomes a routine without explicit approval.

## 13. Context Privacy and Reasoning Transparency

### Goal

Make smarter behavior inspectable without exposing private information.

For "Fix this," show that the planner may consider the focused application but
does not read clipboard contents, browser page contents, screen pixels, disabled
window titles, file contents, or unapproved directories.

For "Use my usual browser," show the approved preference that influenced the
choice.

### Done when

- Request breakdown lists context sources and redacted summaries.
- Sensitive context is opt-in and purpose-limited.
- Planner prompts and audit logs do not contain raw secrets.
- Users can disable individual context providers.
- Missing context causes clarification rather than invention.

## 14. Recovery, Undo, and Interrupted Work

### Goal

Keep planned actions trustworthy when real conditions change.

### Examples

- Cancel during a multi-step file plan.
- Close the application while confirmation is pending.
- Restart Windows during a timer or operation.
- Lose the destination drive after preview.
- Revoke permission between plan steps.
- Ask "What changed?" after a partial failure.
- Ask "Undo that" after a supported move or rename.

### Done when

- The Assistant states exactly which steps completed.
- Pending confirmations do not survive restart.
- Supported rollback is explicit; unsupported undo is not promised.
- Restart never silently resumes a destructive operation.
- Audit history contains enough detail for manual recovery.

## Durable Scheduling and Bounded Autonomy

### Automated lane

1. Create a reminder, restart the scheduler runtime, advance an injected clock
   past its due time, and verify one durable occurrence and one notification.
2. Exercise minute/hour/day/week/month recurrence across month ends, leap days,
   DST gaps and folds, and a 30-day simulated clock run.
3. Start two store instances against the same SQLite database and verify one
   occurrence and one lease claim.
4. Authorize one exact low-risk skill and frozen macro, then change arguments,
   file policy, skill availability, schema/version snapshot, schedule revision,
   and machine identity; every changed envelope must block or require review.
5. Verify medium/high/critical occurrences wait, reject and expire without side
   effects, and execute only after fresh confirmation; critical work retains the
   typed token.
6. Crash-reconcile leased/evaluating work to pending and running work to unknown;
   verify unsafe work is not replayed and safe retry never exceeds its cap.

### Packaged Windows lane

1. Install with **Start Local Assistant with Windows**, sign out/in, and verify
   the tray runtime starts without administrator rights.
2. Create a reminder, hide the window, and verify one nonblocking Windows
   notification and one unread durable Inbox entry. Click the notification and
   verify the exact item/run opens.
3. Restart before a second reminder and sleep past a third; verify the configured
   run-once/misfire behavior and no duplicate notification.
4. Review, approve, reject, revoke, pause, resume, run now, delete, and clear
   completed history from **Automations** using keyboard and mouse.
5. Complete a report-producing automation and verify its Inbox item opens the
   separate results window without resizing or replacing the Assistant. Repeat
   with an image while the Assistant is compact and on a second monitor.
6. Enable overnight quiet hours and verify ordinary interruptions are suppressed
   while Inbox entries persist and error notifications remain visible.
7. Trigger Emergency Stop during a cancellable background job and verify new
   claims pause before the active job is canceled.
8. Run the 24-hour soak and record memory, queue depth, duplicate count, database
   growth, notification behavior, and shutdown thread/task state in the run log.

The packaged, sleep, notification, startup, accessibility, and real-time soak
steps require the target Windows session and remain manual release evidence.

## Documents, visual understanding, and sources

1. Extract each supported document format; verify file/size/character/count
   bounds, encrypted/malformed failures, and page/sheet/slide/line locators.
2. Summarize, compare, answer a question, and find action items across explicit
   documents; verify every grounded claim can expose its opaque source reference.
3. Build a one-folder index, restart, modify/delete files, search again, inspect
   state, and clear it; verify source files are unchanged and retention requires
   its separate capability.
4. OCR, describe, explain a chart, and explain an error from one explicit image;
   verify no screen capture, clipboard, input, or implicit-retention route exists.
5. Inspect and copy response sources from keyboard and mouse at 100%, 150%, and
   200% display scaling.

Done when contract/unit/UI tests pass offline and the physical display-scaling
lane records no clipped source or confirmation control.

## Exact productivity writes

Exercise Drive and OneDrive non-overwriting download/upload; Outlook draft then
separate exact-draft send; exact calendar create/update/delete; exact Slack
channel/thread post; exact Notion parent creation/append; and exact GitHub issue
creation/comment. For every operation verify schema rejection, fixed origin,
minimum scope, exact preview/target, cancellation, offline lock, confirmation,
non-retry after unknown outcome, and absence of unsupported bulk/destructive
actions. Use fake transports in automation and test accounts only in the manual
lane.

## Event-triggered automation

1. File: create/modify/delete matching and nonmatching fixtures in one approved
   folder; verify bounded metadata and deduplication.
2. Process: start/stop one exact benign process; verify only name/PID is retained.
3. Device: feed status changes for one exact instance ID; verify bounded status
   and fingerprint without raw payload persistence.
4. Web: change bounded content at one public HTTPS fixture; verify credential,
   query, fragment, redirect/private-address, offline, and timeout behavior.
5. Calendar: submit typed connector observations and verify no unconfigured
   account polling is implied.
6. For all sources, change the monitor or action digest and verify authority is
   revoked before the frozen action executes.

## Guided UX and accessibility

Complete the resumable ten-step setup; force configuration, permission,
provider, file, network, and integrity failures and verify categorized recovery.
Test keyboard-only operation, accessible names, text scales through 175%, high
contrast, reduced motion, source inspection/copy, confirmation, cancellation,
and emergency stop. Offscreen UI tests are automated; Narrator, physical display
scaling, audio-device, and real focus behavior require an interactive session.

## Evaluation, provenance, and release

1. Run deterministic and full production-planner corpora and retain versioned
   JSON/Markdown reports with per-category scores and confidence intervals.
2. Verify correlated metrics contain timings/counts/categories but no prompt,
   transcript, response, credential, document, or source-body text.
3. Run Ruff, selected strict mypy, regression/learning/offscreen UI coverage,
   property/architecture tests, documentation consistency, and dependency audit.
4. Build the executable and installer where Inno Setup is available; run the
   packaged diagnostic smoke and media dependency check.
5. Generate CycloneDX SBOM, artifact hashes, source-tree hash, exact commit/dirty
   state, SHA256SUMS, and release manifest. A dirty manifest is internal evidence
   only; a public candidate must be generated from a clean tagged commit.
6. Complete the licensed speech corpus, physical microphone/speaker, clean
   install/upgrade/uninstall, Authenticode signing, sleep/toast, and 24-hour soak
   gates from `RELEASE_AND_SIGNING_RUNBOOK.md`. Record `BLOCKED`, never `PASS`,
   when the necessary external asset/environment is absent.

## Recommended Execution Order

### P0: Product credibility

1. Voice Command Acceptance.
2. Safe File Operation.
3. Permission and Confirmation.
4. Goal Interpretation and Clarification.
5. Contextual Follow-Up.
6. Adaptive Multi-Step Planning.
7. Local Model Fallback.
8. Packaged Install.

### P1: Daily usefulness

9. Browser Utility.
10. Notes, Tasks, and Memory.
11. Local Troubleshooting.
12. Routine and Macro Creation.
13. Recovery and Undo.

### P2: Expansion after the bounded release slices are solid

- Office document editing and generated spreadsheets/slide decks.
- Live screen/camera understanding and image generation/editing.
- Additional email/calendar/messaging providers and broad synchronization.
- Travel, shopping, financial, and broader smart-home actions.

These broader workflows remain outside the release claim until they have
dedicated schemas, threat reviews, data-access permissions, and acceptance
evidence. The bounded document, explicit-image, provider-write, and event-source
slices above are part of the current release candidate.

## Interactive Run Log

Actual UI test results should be recorded separately in
`docs/ACCEPTANCE_RUN_LOG.md` so this roadmap remains stable and reusable.
