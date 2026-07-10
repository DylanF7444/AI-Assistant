# Architecture

## Data flow

1. A push-to-talk source emits mono 16 kHz PCM and pipeline events.
2. An interchangeable offline STT provider emits immutable raw and normalized
   transcripts.
3. Deterministic routing handles common commands. Only unmatched requests may
   reach a local schema-constrained planner.
4. The selected registered skill validates its Pydantic input schema.
5. Policy checks user, skill, capability, session, and target scopes.
6. Sensitive calls produce an immutable, expiring confirmation instead of
   executing.
7. The execution engine revalidates and reauthorizes immediately before work.
8. Structured results produce display and optional speech responses.
9. Permission decisions and outcomes are written to a separate audit log.

For unmatched conversational requests, enabled local knowledge bases are
searched with bounded lexical retrieval. Relevant excerpts are included as
untrusted reference data in the schema-constrained planner prompt. Knowledge
files are stored as local database content and never execute instructions.

The UI depends on the orchestrator and event bus, not model or OS
implementations. STT, LLM, TTS, wake-word, embedding, Windows, storage, clock,
and device implementations are injected behind typed interfaces.

The implemented context-aware routing and planning roadmap is documented in
`docs/SMART_ASSISTANT_IMPLEMENTATION_PLAN.md`. It records the design history
for semantic retrieval, context snapshots, intent interpretation, preference
memory, and adaptive replanning while preserving the permission-gated
execution model described above.

## Compatibility

Core contracts and skill manifests use API version `1.0`. Minor core releases
must preserve existing fields and meanings. A breaking contract change requires
a new major API version and side-by-side loader support or a documented
migration. Stable skill IDs are never inferred from display names.

## Module boundaries

- `assistant.router.handlers` owns ordered deterministic command families;
  `DeterministicRouter` remains the precedence coordinator and public facade.
- `assistant.orchestration` owns conversation state, file resolution, planner
  coordination, interaction recording, and voice confidence gating.
- `assistant.execution.evaluation` provides the shared typed evaluation used
  by actions, plans, and final execution reauthorization.
- `assistant.windows.interfaces` contains platform protocols. Capability
  modules contain implementations, while the former adapter and skill modules
  preserve legacy imports.
- `assistant.ui.main` lazily loads the optional desktop package, whose workers,
  controller, tab metadata, and application startup are independently testable.
- `assistant.bootstrap` is the composition root and uses named builders for
  storage, retrieval, registry, context, and audio services.

The dependency direction remains contracts/interfaces -> policy and domain
services -> orchestration -> UI/composition. Model output never calls platform
adapters directly.
