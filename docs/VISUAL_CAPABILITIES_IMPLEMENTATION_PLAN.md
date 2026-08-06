# Visual Capabilities Implementation Plan

## Objective

Make visual output a first-class assistant capability while preserving the
existing photo, audio, and video presentation pipeline. The assistant should
be able to choose a visual response when a diagram, chart, timeline, or table
communicates an answer more clearly than prose, and every visual should have a
text and speech-safe fallback.

The implementation is intentionally additive. Existing `media.present`,
`media.viewer_control`, media sessions, codecs, permissions, audio focus, and
results-window behavior remain responsible for media and are not repurposed for
structured visuals.

## Product decisions

1. Structured visuals are preferred for data and explanations. The assistant
   produces a typed visual specification and the desktop renders it locally.
2. Visual output is separate from media. A chart is not persisted as a fake
   image file, and a visual request never enters the media resolver.
3. The first release is dependency-free and offline. It uses the existing
   PySide6 results window and `QPainter`; no browser, web search, image model,
   or external charting package is required.
4. Visuals are foreground-only and low risk. They do not read files, access the
   network, control windows, or change device state.
5. The response always carries an accessible `alt_text` and a concise display
   and speech summary. A terminal-only or non-Qt caller still receives useful
   output.
6. Natural language is the primary interface, not a mini command language.
   Typed requests and normalized speech transcripts use the same parser, and
   punctuation is optional wherever labels, values, times, or step words make
   the structure inferable.

## Release 1 — implemented slice

The highest-value baseline is now implemented:

| Capability | Natural request example | Output |
| --- | --- | --- |
| Charts | “Chart monthly sales—January twelve, February eighteen, March fifteen” | Bar chart; asking for a line or trend produces a line chart |
| Flowcharts | “Draw the flow: plan, then build, then test, then deploy” | Connected labeled nodes and arrows |
| Timelines | “Show a timeline: nine AM standup, ten AM build, two PM review” | Ordered event cards on a timeline |
| Tables | “Make a table with columns name and status, rows Alice ready, Bob waiting” | Header and rows rendered as a table |

Implementation boundaries:

- `VisualArtifact` and typed chart, flowchart, timeline, and table specs are
  immutable, bounded, control-character-safe contracts.
- `core.visualize` is registered as a low-risk, retry-safe local skill with no
  permissions.
- Explicit visual intent routes deterministically, so common conversational and
  dictated visual requests do not require the local planner.
- `core.visualize` accepts either an ordinary-language `request` or a precise
  typed `spec`. Both forms converge on the same typed artifact contract.
- A shared parser accepts interchangeable punctuation and speech forms such as
  `January=12`, `January: 12`, “January is twelve,” “January at twelve,” or
  simply “January twelve.” It also recognizes spoken step, time, column, and
  row cues.
- Planner-generated `core.visualize` calls use the same schema and execution
  path as deterministic requests.
- The artifact is propagated through `SkillResult`, `Response`, plan
  aggregation, and logic workflow wrapping.
- The results window has a separate visualization page. It opens automatically
  for a successful visual response while the normal assistant response remains
  visible in the conversation.
- The renderer has a text fallback through `alt_text`, `display_text`, speech,
  and the existing terminal output path.

## Target architecture

```text
Typed request or normalized voice transcript
    |
    v
Shared visual-intent and natural-language parser, or local planner
    |
    v
core.visualize + schema validation
    |
    v
ExecutionEngine / normal audit and cancellation boundary
    |
    v
SkillResult.visual_artifact
    |
    v
response_from_result -> Response.visual_artifact
    |                         |
    |                         +--> text/speech/terminal fallback
    v
DesktopController / ResultWindow
    |
    v
VisualizationView (local QPainter renderer)
```

The visual contract is deliberately independent from `MediaDirective`. This
keeps media lifecycle events, stale-file checks, audio focus, remote cache
policy, and viewer controls unchanged.

## Roadmap after Release 1

### Release 1.1 — visual polish and data ergonomics

- Add explicit axis labels, legends, units, numeric formatting, and color-safe
  palettes to chart specs.
- Add a copyable data view and an export action for SVG/PNG/PDF where the user
  explicitly requests an artifact.
- Add keyboard navigation, zoom-to-fit, and high-contrast rendering tests.
- Add visual response cards to the compact assistant view, with an explicit
  “open full visualization” action instead of making the full window mandatory
  for every visual.
- Add direct conversion of existing typed result items into tables when the
  user says “put these results in a table.”

### Release 2 — explain and transform existing visual information

- OCR and bounded layout understanding for screenshots, scanned pages, and
  PDFs.
- Chart explanation, page comparison, region selection, and annotations.
- Image markup operations: crop, highlight, arrows, labels, redaction, and
  before/after comparison.
- Video storyboard, event markers, captions, and frame selection.

These features should use explicit input attachments or approved local files,
bounded decoding, and separate permissions where file or network access is
needed. They should not be added by weakening `media.present` validation.

### Release 3 — interactive visual workspaces

- Pan/zoom/select interactions for diagrams and large tables.
- Interactive dashboards and adjustable simulations for explicitly supplied
  data.
- Maps and geographic timelines with a dedicated network and location policy.
- Screen and camera understanding, with explicit session indicators, pause,
  retention, and privacy controls.
- Safe screen guidance and computer-use overlays, keeping consequential input
  behind the existing confirmation and foreground-only rules.

## Minimal-interference safeguards

- Preserve `MediaDirective` and all media viewer controls as separate types and
  code paths.
- Keep the visualization handler intent-scoped: only requests that explicitly
  ask for a chart, graph, plot, flowchart, diagram, timeline, or table are
  routed locally. Do not require a fixed verb, prefix, punctuation style, or
  label/value separator.
- Leave generic `show`, `view`, file, application, and media requests on their
  existing handlers.
- Exclude media paths and explanatory questions from deterministic visual
  routing, even when a filename or question contains words such as “diagram”
  or “chart.”
- Do not add visual artifacts to the media session registry or audio-focus
  coordinator.
- Keep visual data in memory only. Do not persist titles, labels, rows, or
  generated renderings unless a future explicit export action is added.
- Keep result aggregation backward-compatible: the latest visual artifact is
  preserved only when a plan or workflow actually produced one.
- If the desktop renderer is unavailable, response contracts remain usable by
  terminal, tests, and other clients through their text fields.
- Do not make the local planner a prerequisite for the common visual commands.

## Safety and privacy requirements

- Enforce maximum counts and lengths for labels, nodes, edges, events, rows,
  columns, and text fields.
- Reject non-finite chart values and malformed graph references.
- Treat all model-provided visual text as untrusted data; draw it as text and
  never interpret it as HTML, JavaScript, a file path, or a command.
- Keep visuals low-risk and foreground-only until a later capability has a
  documented permission model.
- Preserve sensitive-response behavior: if the source result is sensitive,
  the visual must be hidden or replaced by the existing privacy fallback.
- Provide a text equivalent for every visual, including when Qt is absent.
- Do not log full visual payloads in routine audit or terminal diagnostics.

## Verification plan

### Contract and routing tests

- Valid round-trip serialization for every visual kind.
- Immutability, bounds, control-character rejection, finite-value checks, and
  graph/table consistency checks.
- Response propagation and plan/workflow preservation.
- Deterministic positive routes and negative regression checks for existing
  media/file/application commands.
- Equivalent typed separators, punctuation-free dictation, spoken numbers and
  times, polite phrasings, visual nouns at the end of requests, and ambiguous
  input clarification.
- End-to-end normalized `Transcript` coverage proving that speech-to-text takes
  the same visual path without requiring literal punctuation.

### Desktop tests

- Offscreen paint tests for chart, flowchart, timeline, and table artifacts.
- Accessible description and text fallback checks.
- Results-window switching without changing the existing media page.
- Existing media viewer tests remain green.

### Manual acceptance requests

These requests exercise capabilities; their wording and punctuation are not
required syntax. Change polite phrases, capitalization, separators, or spoken
number forms while preserving the same meaning.

| Ability under test | Example request | Expected result |
| --- | --- | --- |
| Conversational chart | “Could you chart monthly sales for January twelve, February eighteen, and March fifteen?” | Three correctly labeled bars |
| Separator independence | Try `Jan=12`, `Jan:12`, “Jan is twelve,” “Jan at twelve,” and “Jan twelve” | Equivalent labels and values |
| Spoken punctuation | “Make a chart monthly sales colon January colon twelve February equals sign eighteen” | Title and two values parsed; punctuation words are not drawn as labels |
| Chart at sentence end | “Turn January twelve and February eighteen into a chart” | Two-point chart without a command prefix |
| Trend selection | “Show a line chart for Monday 10, Tuesday 14, Wednesday 12” | Line chart rather than bars |
| Linear process | “Please draw the release flow: plan, then build, then test, then deploy” | Four connected steps |
| Decision process | “Draw the flow: start, then review, then if approved then ship otherwise revise” | Yes/no edges from an approval decision |
| Dictated timeline | “I would like a timeline nine AM standup ten AM build two PM review” | Three timed events without required punctuation |
| Dictated table | “Make a table with columns name and status and rows Alice ready Bob waiting” | Two columns and two rows |
| Spoken table punctuation | “Make a table name pipe status semicolon Alice pipe ready” | One header row and one data row |
| Ambiguity handling | “Make a table from customer information” | Clarification; no guessed table is rendered |
| Media isolation | “Show `C:\Pictures\diagram.png`” | Existing media viewer, not `core.visualize` |
| Explanation isolation | “How does a flowchart work?” | Conversational/planner answer, not a rendered flowchart |

Ways to test:

1. Enter requests in the normal text box and confirm the results window opens
   the correct visual page, labels and values match, and the conversation keeps
   a useful text description.
2. Speak the punctuation-free and spoken-punctuation variants through each
   configured STT provider. Confirm the normalized transcript creates the same
   artifact as typed input. Repeat with natural pauses and filler-free polite
   phrasing rather than trying to dictate symbols.
3. Resize the results window and inspect long labels, negative/decimal chart
   values, multiple timeline events, and a table near its visible capacity.
4. Repeat the media and explanatory negative cases to verify existing routing
   has not changed. Also exercise media playback controls after viewing a
   visual; visuals must not create or replace a media session.
5. Turn on sensitive-result display protection in a test configuration and
   verify a sensitive visual is replaced by the normal privacy fallback.
6. Run the automated contract, routing, transcript, orchestration, audit, and
   offscreen renderer suites below.

### Release commands

```powershell
python -m pytest tests/test_visualization.py tests/test_media_contracts.py tests/test_response.py -q
python -m pytest -m ui tests/ui/test_visualization_view.py tests/ui/test_media_viewer.py -q
python -m pytest
python -m pytest learning_tests -v
python -m ruff check src/assistant/contracts.py src/assistant/skills/visualize.py src/assistant/router/handlers/visualization.py src/assistant/visualization/parser.py src/assistant/ui/desktop/visualization_view.py
```

## Explicitly deferred

The first release does not add generic web image search, OCR, document
understanding, image generation/editing, live camera/screen vision, maps,
3D/AR, spreadsheet editing, or automatic export. Each requires a distinct
input, privacy, permission, and packaging review.
