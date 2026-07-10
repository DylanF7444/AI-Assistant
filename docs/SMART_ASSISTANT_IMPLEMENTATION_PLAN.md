# Smart Assistant Implementation Plan

Updated July 7, 2026.

## Implementation status

The core roadmap below is implemented. Broad-goal routing, structured intent
interpretation, budgeted context, semantic retrieval with lexical fallback,
skill examples, two-pass planning, bounded replanning, explicit preference
memory, and orchestrator integration are present and covered by regression
tests.

The phase descriptions remain as design history and acceptance rationale. The
remaining backlog is limited to the open design questions, external hardware
and model benchmarking, and integrations explicitly listed as non-goals.

This document records the architecture and execution phases used to make Local
Assistant more context-aware, adaptive, and intent-driven. It remains the
reference rationale for semantic retrieval, context snapshots, richer intent
interpretation, preference memory, and adaptive replanning.

## Goal

Local Assistant should first understand the user's situation, then decide what
to do. The current architecture is safe and capable, but it can feel mechanical
because much of the system maps utterances directly to deterministic routes,
registered skills, or one-shot planner decisions.

The target behavior is:

1. Understand the user's goal, not just the command wording.
2. Gather relevant local context before choosing an action.
3. Retrieve semantically related skills, memories, knowledge, and workflows.
4. Ask clarifying questions when intent, target, or success criteria are
   ambiguous.
5. Produce safe, inspectable plans with explicit assumptions and constraints.
6. Execute through the existing permission, confirmation, and audit pipeline.
7. Observe results and revise the plan when conditions change.

The safety model remains unchanged: model output is untrusted, registered
skills are the only action mechanism, permissions are default-deny, sensitive
actions require confirmation, and all executable calls are schema validated.

## Current Baseline

Current high-level flow:

```text
transcript/text
-> deterministic router
-> local schema-constrained planner for unmatched requests
-> skill validation
-> policy and confirmation
-> execution
-> response
```

Current limitations:

- Deterministic routing has broad authority over many utterances, so partially
  matched commands may bypass deeper interpretation.
- The local planner primarily chooses a skill, answer, rejection, clarification,
  or concrete multi-step plan.
- Relevant-skill retrieval is lexical token overlap.
- Session context is bounded and short-lived.
- Local knowledge retrieval is lexical.
- There is no first-class contract for inferred user goal, implicit needs,
  constraints, assumptions, or success criteria.
- There is no durable, permission-gated preference memory separate from raw
  transcript history.
- Plans generally require concrete arguments up front and do not yet support a
  formal observe/replan loop.

## Target Architecture

Target high-level flow:

```text
transcript/text
-> fast control router
-> context snapshot
-> semantic retrieval
-> intent interpretation
-> plan or tool selection
-> policy and confirmation
-> execution
-> result observation
-> optional replan
-> response
```

The main new artifacts are:

- `ContextSnapshot`: structured local context available for reasoning.
- `RetrievedContext`: semantically relevant skills, examples, memories,
  knowledge excerpts, workflow templates, and recent history summaries.
- `UserIntentInterpretation`: the assistant's explicit understanding of the
  user's goal.
- `ActionPlan`: executable, permission-gated skill calls or a clarification.
- `PlanRevision`: a bounded replanning decision after observing a result.

## Design Principles

- Keep deterministic routes for high-confidence, low-ambiguity commands.
- Send broad, goal-like, or underspecified requests to the understanding layer.
- Prefer structured context over prompt prose.
- Collect cheap context broadly, collect sensitive context only with explicit
  permission or a clear user request.
- Keep model-generated plans non-executable until converted into validated
  `SkillCall` or `ExecutionPlan` objects.
- Make the assistant's interpretation inspectable in developer/debug views.
- Treat semantic retrieval as advisory; policy and schemas remain authoritative.
- Do not include raw sensitive values in logs unless existing redaction policy
  explicitly permits them.

## Pre-Implementation Guardrails

Before large architecture work begins, stabilize the current worktree and land
the first implementation in a deliberately small milestone:

1. Commit or intentionally set aside unrelated local changes.
2. Start with broad request routing, a context snapshot shell,
   `UserIntentInterpretation`, and clarification behavior.
3. Add scenario tests before broad wiring so "smartness" is measured through
   concrete user situations rather than architecture alone.

The first milestone should prove this flow:

```text
broad request
-> context snapshot
-> intent interpretation
-> clarification or safe plan
```

Example:

```text
User: Clean up my downloads.
Assistant: Should I organize Downloads by file type, date, or project/name?
```

No files are moved in the first milestone.

## Phase 1: Understanding Contract

Add a new module:

```text
src/assistant/language/understanding.py
```

Add a structured interpretation model:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UserIntentInterpretation(BaseModel):
    user_goal: str
    request_type: Literal[
        "conversation",
        "question",
        "local_action",
        "multi_step_goal",
        "automation",
        "preference_memory",
        "unsupported",
    ]
    explicit_requests: list[str] = Field(default_factory=list)
    implicit_needs: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    relevant_context_keys: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    should_ask_clarification: bool = False
    clarification_question: str | None = None
    confidence: float = Field(ge=0, le=1)
```

Example:

```json
{
  "user_goal": "Organize files in the Downloads folder",
  "request_type": "multi_step_goal",
  "explicit_requests": ["clean up downloads"],
  "implicit_needs": [
    "inspect folder contents",
    "choose an organization rule",
    "avoid deleting user files"
  ],
  "missing_information": ["preferred organization method"],
  "assumptions": [],
  "constraints": ["preview before moving files"],
  "relevant_context_keys": ["known_folders.downloads", "file_policy"],
  "risk_notes": ["moving files changes filesystem state"],
  "success_criteria": [
    "files are grouped predictably",
    "no files are deleted",
    "user approves the final move plan"
  ],
  "should_ask_clarification": true,
  "clarification_question": "Should I organize Downloads by file type, date, or project/name?",
  "confidence": 0.86
}
```

Acceptance criteria:

- Broad requests produce an interpretation before skill selection.
- Interpretations are schema validated.
- Clarification can be requested because intent is ambiguous, not only because
  a skill argument is missing.
- Existing one-shot planner tests continue to pass unless deliberately updated.

## Phase 2: Router Fast Path

Keep deterministic routing for:

- `cancel`, `stop`, `undo`, `yes`, `no`, `help`
- date and time
- simple timer commands
- simple calculator commands
- simple volume, brightness, Wi-Fi, Bluetooth, media, and power commands
- simple app launch and browser navigation
- direct contextual file result selection

Send broad or goal-like requests to the understanding layer:

- "clean up..."
- "organize..."
- "help me..."
- "prepare..."
- "figure out..."
- "make this easier..."
- "what should I..."
- "compare..."
- "summarize..."
- "rewrite..."
- "plan..."
- "fix this..."
- "remember that..."
- "use my usual..."

Add a broad-request helper in `src/assistant/router/deterministic.py`:

```python
def _looks_like_goal_request(text: str) -> bool:
    return bool(
        re.search(
            r"\b("
            r"help me|clean up|organize|prepare|figure out|make .* easier|"
            r"what should i|compare|summarize|rewrite|plan|fix this|"
            r"remember that|use my usual"
            r")\b",
            text,
            re.I,
        )
    )
```

When true, return a planner route:

```python
RouteResult(
    Intent(
        name="planner.goal",
        arguments={"text": text},
        confidence=0.7,
        source="classifier",
        ambiguous=True,
        ambiguity_reasons=("Broad goal request requires interpretation",),
    ),
    needs_llm=True,
)
```

Acceptance criteria:

- High-confidence control commands remain deterministic.
- Broad requests do not get prematurely converted into a single action.
- Tests cover at least five broad requests that route to the planner.

## Phase 3: Context Snapshot System

Add:

```text
src/assistant/context/
  __init__.py
  snapshot.py
  providers.py
  budget.py
```

Initial contracts:

```python
from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ContextItem(BaseModel):
    key: str
    title: str
    value: Any
    sensitivity: Literal["public", "local", "sensitive"] = "local"
    source: str
    summary: str = ""


class ContextSnapshot(BaseModel):
    request_text: str
    session_id: str
    items: list[ContextItem] = Field(default_factory=list)


class ContextProvider(Protocol):
    name: str

    async def collect(
        self,
        request_text: str,
        *,
        session_id: str,
        budget: "ContextBudget",
    ) -> list[ContextItem]:
        ...
```

The snapshot should be structured and budgeted. Avoid dumping arbitrary objects
or full file contents into prompts.

### Always-Cheap Context

Collect for most planner requests:

- Recent session request/response summaries.
- Pending clarification state.
- Selected file and recent file candidates.
- Active long-running job or plan state.
- Available skill directory.
- Permission and confirmation summary.
- Offline/cloud availability.
- Model/provider health if already known or cheap.

### Intent-Triggered Context

Collect only when relevant:

- File requests:
  - known user folders
  - selected file
  - recent file candidates
  - approved folder summaries
  - file access policy summary
- App/window requests:
  - focused application
  - visible/open windows
  - registered applications
  - recently launched apps
- Browser requests:
  - active browser if known
  - current URL/title only with permission or explicit browser context request
- Device requests:
  - audio devices
  - battery status
  - display brightness
  - Wi-Fi/Bluetooth state
  - connected devices
  - smart-home entities if configured
- Writing/text requests:
  - selected text or clipboard summary only with permission or explicit request
- Development requests:
  - known project folders
  - current project state if the user explicitly asks about a project

### Sensitive Context

Sensitive context requires explicit permission, explicit user request, or a
previously granted scoped permission:

- clipboard contents
- screen screenshots or OCR
- browser page contents
- file contents
- email, calendar, messages
- credentials, tokens, secrets
- private document bodies

Context collection must also honor the application settings. The initial
settings flags are:

- `context_include_open_applications`: allow non-sensitive open/focused
  application context.
- `context_include_connected_devices`: allow non-sensitive connected-device
  and device-state context.
- `context_allow_clipboard`: allow clipboard context when the relevant
  clipboard permission and user intent are present.
- `context_allow_browser`: allow browser URL/title/page context when the
  relevant permission and user intent are present.
- `context_allow_screen`: allow screen capture/OCR context when the relevant
  permission and user intent are present.
- `context_allow_file_contents`: allow file-content context when filesystem
  permission and explicit user intent are present.

These flags are not a replacement for permissions. They are an additional
privacy boundary: if a flag is disabled, the corresponding context provider
must not collect or expose that context even if a lower-level capability exists.

Acceptance criteria:

- `ContextSnapshot` is available before planner calls.
- Context collection is modular and testable.
- Sensitive context is excluded by default.
- Context providers obey the settings flags above.
- Debug output can show which context keys were used without leaking sensitive
  values.

## Phase 4: Context Providers

Implement providers incrementally.

### SessionContextProvider

Sources:

- `AssistantOrchestrator._session_context`
- pending clarification follow-up
- selected file and file candidates

Output examples:

- `session.recent_interactions`
- `session.pending_clarification`
- `files.selected`
- `files.recent_candidates`

### SkillContextProvider

Sources:

- `SkillRegistry.list()`

Output examples:

- `skills.available`
- `skills.risk_summary`
- `skills.permission_summary`

This provider can feed both prompt context and semantic retrieval indexing.

### PermissionContextProvider

Sources:

- `PermissionManager`
- file access policy store
- confirmation settings

Output examples:

- `permissions.granted`
- `permissions.denied`
- `permissions.file_policy`
- `permissions.confirmation_required`

### DesktopContextProvider

Sources:

- `WindowsApplicationAdapter`

Output examples:

- `desktop.focused_application`
- `desktop.visible_windows`
- `desktop.monitor_layout`

Use friendly app/window names when possible. Avoid including window titles if
they could expose private document names unless permitted.

### ProcessContextProvider

Sources:

- `WindowsProcessAdapter`

Output examples:

- `processes.user_apps`
- `processes.high_resource`

Filter noisy background processes before sending to the planner.

### DeviceContextProvider

Sources:

- `LocalWindowsDeviceAdapter`
- configured device integrations

Output examples:

- `devices.audio`
- `devices.battery`
- `devices.display`
- `devices.network`
- `devices.bluetooth`
- `devices.connected`
- `devices.smart_home`

This directly addresses the requirement to include relevant information such as
open applications and connected devices.

### FileContextProvider

Sources:

- `LocalFileSystemAdapter`
- selected file state
- file search results
- explicit user folder references

Rules:

- Summarize folders by counts, extensions, date ranges, and names only when
  permitted.
- Do not read file contents unless explicitly requested and allowed.
- Use file access policy before inspecting paths.

### PreferenceContextProvider

Sources:

- new memory/preference store

Output examples:

- `memory.preferences`
- `memory.constraints`
- `memory.projects`
- `memory.workflows`

### KnowledgeContextProvider

Sources:

- existing `KnowledgeBaseStore`
- future vector index

Output examples:

- `knowledge.excerpts`
- `knowledge.sources`

Acceptance criteria:

- Providers can be called independently in tests.
- Provider failures degrade gracefully and do not fail the whole planner unless
  the requested context is essential.
- Context items include source and sensitivity metadata.

## Phase 5: Semantic Retrieval

Replace or augment the lexical `SkillRetriever` with semantic retrieval.

Add:

```text
src/assistant/language/embeddings.py
src/assistant/storage/vector_index.py
src/assistant/language/retrieval.py
```

Embedding interface:

```python
from __future__ import annotations

from typing import Protocol


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...
```

Initial providers:

- `OllamaEmbeddingProvider`
- `LexicalFallbackEmbeddingProvider` or existing lexical search fallback

Start with a simple SQLite-backed vector table and linear cosine search. This
is acceptable for early local scale and avoids adding an ANN dependency too
early.

Suggested table:

```text
vector_items
- id integer primary key
- namespace text not null
- source_id text not null
- text text not null
- metadata_json text not null
- embedding_blob blob
- embedding_model text
- updated_at datetime not null
```

Namespaces:

- `skill`
- `skill_example`
- `knowledge`
- `memory`
- `history_summary`
- `workflow_template`
- `file_summary`

Retrieval API:

```python
class RetrievedContextItem(BaseModel):
    namespace: str
    source_id: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float


class SemanticRetriever:
    async def search(
        self,
        query: str,
        *,
        namespaces: set[str],
        limit: int = 8,
    ) -> tuple[RetrievedContextItem, ...]:
        ...
```

Index these artifacts:

- Skill manifests:
  - skill ID
  - display name
  - description
  - schema summary
  - risk and permissions
- Skill examples:
  - user utterance
  - target skill
  - arguments
  - notes
- Knowledge chunks:
  - existing enabled knowledge bases
- Memories and preferences:
  - user-approved preferences
  - project facts
  - constraints
- History summaries:
  - successful workflows
  - unresolved questions
  - recent corrected interpretations
- Workflow templates:
  - organize folder
  - prepare meeting
  - summarize document
  - troubleshoot app
  - find recent work
  - draft message

Acceptance criteria:

- Skill retrieval works when wording does not lexically overlap with the skill
  manifest.
- Lexical fallback remains available when embeddings are unavailable.
- Retrieved items include namespace, source, score, and metadata.
- Retrieval respects disabled knowledge bases and memory deletion.

## Phase 6: Skill Examples

Semantic retrieval improves significantly when skills have examples.

Add either:

- optional `examples` to skill manifests in a future API-compatible way, or
- sidecar example registry under `src/assistant/skills/examples.py`.

Example contract:

```python
class SkillExample(BaseModel):
    utterance: str
    skill_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    requires_clarification: bool = False
    clarification: str | None = None
```

Examples for `windows.file_operation`:

```text
Utterance: clean up my downloads
Skill: none yet
Clarification: Should I organize Downloads by file type, date, or project/name?
Notes: Broad filesystem request; inspect and preview before moving.

Utterance: move the first result to Documents
Skill: windows.file_operation
Arguments: {"operation": "move", "source": "the first result", "destination": "documents"}

Utterance: organize PDFs into a folder
Skill: none yet
Clarification: Which folder should I organize?
Notes: Requires file search/list and a preview before moves.
```

Examples for `windows.application_window`:

```text
Utterance: put Brave on my second monitor
Skill: windows.application_window
Arguments: {"operation": "move_to_monitor", "process_name": "brave.exe", "monitor": 2}

Utterance: get this out of the way
Skill: windows.application_window
Arguments: {"operation": "minimize_focused"}
Notes: Requires focused window context.
```

Acceptance criteria:

- Examples are indexed into semantic retrieval.
- Examples can represent clarification instead of direct skill calls.
- Existing manifest API compatibility is preserved unless a versioned migration
  is deliberately planned.

## Phase 7: Two-Pass Planner

Modify `LocalPlanner` to support:

```text
interpret -> plan_from_interpretation
```

Proposed public methods:

```python
async def interpret(
    self,
    text: str,
    *,
    context: ContextSnapshot,
    retrieved: tuple[RetrievedContextItem, ...],
) -> UserIntentInterpretation:
    ...


async def plan_from_interpretation(
    self,
    text: str,
    *,
    interpretation: UserIntentInterpretation,
    context: ContextSnapshot,
    retrieved: tuple[RetrievedContextItem, ...],
) -> PlannerDecision:
    ...
```

Planner flow:

```text
1. Build context snapshot.
2. Retrieve relevant skills, examples, memories, knowledge, and workflows.
3. Interpret the request.
4. If clarification is needed, ask it.
5. If no action is needed, answer.
6. If action is needed, retrieve/confirm candidate skills.
7. Produce a schema-valid skill call or executable plan.
8. Prepare through the execution engine.
```

Prompt requirements:

- Clearly separate system rules, retrieved context, and user request.
- Mark knowledge and memory content as untrusted reference data.
- Require the model to list missing information and assumptions.
- Require clarification when a later action depends on unknown runtime data.
- For action planning, allow only candidate skills selected by retrieval.
- Never allow arbitrary shell commands or invented abilities.

Acceptance criteria:

- Broad requests produce a validated interpretation before planning.
- Planner can choose clarification based on intent ambiguity.
- Planner can answer ordinary questions without selecting a skill.
- Planner can still produce direct skill calls for simple LLM-routed requests.

## Phase 8: Dynamic Replanning

Current concrete plans are safe but limited. Add bounded observe/replan support
for workflows where later steps depend on earlier results.

Initial design:

```text
1. Planner produces one executable next step or a clarification.
2. Engine prepares/executes the step.
3. Structured result is summarized.
4. Planner receives original goal, interpretation, context, and result summary.
5. Planner chooses stop, clarify, or next step.
6. Loop stops after a small maximum iteration count.
```

Add a contract:

```python
class PlanRevision(BaseModel):
    disposition: Literal["stop", "next_step", "clarify", "reject"]
    rationale: str
    skill_id: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    clarification: str | None = None
    response: str | None = None
    confidence: float = Field(ge=0, le=1)
```

Safety constraints:

- Maximum iteration count, for example 5.
- Each step is validated and authorized independently.
- Sensitive or destructive actions still require confirmation.
- Result summaries are bounded and redacted.
- The planner cannot reference arbitrary previous output in executable
  arguments. The orchestrator must bind selected values explicitly.

Example:

```text
User: Find the report and put it in my project folder.

Step 1: file search for "report".
Observation: 4 candidates found.
Revision: ask which candidate, or select if user gave enough constraints.
Step 2: move/copy selected file after confirmation.
```

Acceptance criteria:

- Multi-step workflows can depend on search/inspection results.
- Replanning cannot execute unvalidated placeholders.
- Loop limits prevent runaway execution.
- Tests cover success, clarification, rejection, and loop-limit behavior.

## Phase 9: Preference Memory

Add explicit, permission-gated memory separate from transcript history.

Add:

```text
src/assistant/storage/memory.py
```

Memory types:

- `preference`
- `project`
- `identity`
- `workflow`
- `correction`
- `constraint`

Suggested table:

```text
memory_items
- id integer primary key
- type text not null
- key text not null
- value text not null
- source text not null
- confidence real not null
- enabled boolean not null
- created_at datetime not null
- updated_at datetime not null
- last_used_at datetime
```

Examples:

- Preference: "User prefers Brave for browsing."
- Constraint: "Ask before moving or deleting files."
- Project: "Assistant project lives at Desktop/PROJECTS/Assistant."
- Workflow: "For coding changes, run targeted tests before final response."

Commands:

- "Remember that I prefer Brave."
- "Forget that preference."
- "What do you remember about me?"
- "Always ask before organizing files."

Policy:

- Memory writes require explicit user intent.
- Sensitive memories require confirmation.
- The user can list, disable, edit, or delete memories.
- Memory retrieval respects deletion and disabled state.
- Memory should be indexed for semantic retrieval only after approval.

Acceptance criteria:

- Preference memory influences planner interpretation.
- Memory is inspectable and deletable.
- Raw transcript retention remains separate from durable memory.
- Tests cover remember, retrieve, forget, and no-implicit-memory behavior.

## Phase 10: Orchestrator Integration

Update `AssistantOrchestrator.handle_text`:

```text
1. Handle pending clarification follow-up.
2. Run fast deterministic route.
3. If route is deterministic and high confidence, continue existing flow.
4. If route needs LLM:
   a. Build context snapshot.
   b. Run semantic retrieval.
   c. Run planner.interpret().
   d. Ask clarification, answer, reject, or plan.
   e. Prepare through existing engine.
5. Record interpretation metadata in audit/debug output.
```

Do not bypass:

- `Skill.validate`
- `ExecutionEngine.prepare`
- permission checks
- confirmation checks
- audit logging
- rollback/idempotency behavior

Add optional developer display:

```text
Request breakdown:
- User goal
- Request type
- Missing information
- Assumptions
- Relevant context keys
- Retrieved items
- Selected skill/plan
- Result state
```

Acceptance criteria:

- Existing deterministic commands still work.
- Planner-routed requests include context and retrieval.
- Reasoning/debug display shows interpretation without leaking sensitive values.
- Planner failures degrade to current local-planner error behavior.

## Test Plan

Add:

```text
tests/test_understanding.py
tests/test_context_snapshot.py
tests/test_semantic_retrieval.py
tests/test_memory.py
tests/test_replanning.py
```

Scenario tests:

- "Clean up my downloads."
  - Expected: asks organization preference, does not move files.
- "Prepare for my meeting."
  - Expected: asks which meeting or explains calendar integration is unavailable.
- "Find what I was working on yesterday."
  - Expected: uses recent history/project context or asks for scope.
- "Organize these files by project."
  - Expected: asks which files/folder if no selected file context exists.
- "Make this note into tasks."
  - Expected: asks for note/source text if missing.
- "Use my usual browser."
  - Expected: retrieves browser preference or asks to set one.
- "Turn that down."
  - Expected: resolves from recent context when clear, otherwise asks.
- "Compare these options."
  - Expected: asks for options if not provided.
- "Fix this."
  - Expected: asks what "this" refers to unless selected text/screen context is
    explicitly available.

Privacy tests:

- Clipboard contents are not included without permission.
- File contents are not read for a broad file request.
- Screen capture/OCR is not used without permission.
- Browser URL/title/content is omitted unless permitted or explicitly requested.
- Sensitive context values are redacted in debug output.
- Deleted memories are not retrieved or indexed.

Routing tests:

- Fast commands remain deterministic.
- Broad requests route to `planner.goal`.
- Partial deterministic matches do not override broad goal interpretation.

Semantic retrieval tests:

- Skill retrieval works for synonym/semantic wording.
- Skill examples affect retrieval.
- Lexical fallback works when embeddings are unavailable.
- Disabled knowledge bases are not retrieved.

Replanning tests:

- Search result can lead to clarification.
- Clarification answer resumes the original goal.
- Loop limit stops runaway planning.
- Each replanned step goes through validation and permissions.

## Implementation Sequence

Recommended order:

1. Add scenario tests for the first milestone before broad wiring.
2. Add settings-configurable context privacy flags and tests.
3. Add `UserIntentInterpretation` contract and tests.
4. Add `ContextSnapshot`, `ContextItem`, and context budget contracts.
5. Implement session, skill, and permission context providers.
6. Add `LocalPlanner.interpret()` using structured output.
7. Route broad requests to `planner.goal`.
8. Wire context snapshot into LLM-routed requests.
9. Verify the small first milestone end to end: broad request -> context
   snapshot -> interpretation -> clarification or safe plan.
10. Add semantic retrieval for skills and skill examples.
11. Add initial skill examples.
12. Add preference memory store and commands.
13. Add desktop, process, device, and file context providers.
14. Add knowledge/memory/history retrieval indexing.
15. Add `plan_from_interpretation()`.
16. Add bounded dynamic replanning.
17. Expand debug/request breakdown display.
18. Add full scenario, privacy, and replanning tests.

## Milestones

### Milestone 1: Broad Goal Understanding

Deliverables:

- broad-request routing
- `UserIntentInterpretation`
- session/skill/permission context snapshot
- planner interpretation prompt
- clarification based on ambiguous goal

Example success:

```text
User: Clean up my downloads.
Assistant: Should I organize Downloads by file type, date, or project/name?
```

No files are moved.

### Milestone 2: Semantic Skill Retrieval

Deliverables:

- embedding provider interface
- vector index
- skill and skill-example indexing
- semantic retrieval fallback

Example success:

```text
User: Get this window out of the way.
Retrieved: windows.application_window minimize_focused example.
```

### Milestone 3: Context-Rich Planning

Deliverables:

- desktop/process/device/file providers
- prompt budget controls
- relevant context key reporting

Example success:

```text
User: Move Brave to my other monitor.
Context: Brave is open, two monitors are available.
Plan: windows.application_window move_to_monitor.
```

### Milestone 4: Preference Memory

Deliverables:

- memory store
- remember/list/forget behavior
- memory retrieval and indexing

Example success:

```text
User: Remember that I prefer Brave for browsing.
Assistant: I will remember that you prefer Brave for browsing.

User: Open my usual browser.
Assistant: Opens Brave.
```

### Milestone 5: Adaptive Replanning

Deliverables:

- result observation summaries
- `PlanRevision`
- bounded replan loop
- tests for search, clarification, and follow-up execution

Example success:

```text
User: Find the report and put it in my project folder.
Assistant: Finds candidates, asks which report, then prepares the move/copy
after the user selects one.
```

## Open Design Questions

- Which local embedding model should be the default for offline semantic
  retrieval?
- Should vector storage be pure SQLite initially, or should `sqlite-vec` be
  introduced early?
- Should skill examples live in manifests, sidecar registry files, or both?
- What UI should let users inspect and edit durable memories?
- Which context providers should be enabled by default in local-only mode?
- How should screen and browser context permissions be presented to avoid
  accidental over-collection?
- Should broad filesystem operations use a dedicated "preview file operation
  plan" skill?

## Non-Goals For The First Pass

- Autonomous unsupervised desktop control.
- Raw shell command execution from planner output.
- Reading arbitrary file contents during broad organization requests.
- Screen capture or OCR without explicit permission.
- Email, calendar, or messaging integration unless a separate integration skill
  is added.
- Replacing the existing permission and confirmation model.

## Definition Of Done

This plan is considered implemented when:

- Broad user goals produce a validated interpretation before action selection.
- Semantic retrieval is used for skills and at least one other namespace.
- Context snapshots include relevant session, skill, permission, desktop/app,
  file, and device information when appropriate.
- Preference memory is explicit, inspectable, and deletable.
- The assistant asks clarifying questions for ambiguous goals and unsafe
  assumptions.
- Multi-step workflows can observe a result and replan within strict limits.
- Scenario tests demonstrate improved behavior for nuanced requests.
- Privacy tests prove sensitive context is not collected or exposed by default.
