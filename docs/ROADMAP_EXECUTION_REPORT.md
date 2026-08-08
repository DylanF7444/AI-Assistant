# Roadmap Execution Report

Updated August 6, 2026.

This report is the completion ledger for
`ASSISTANT_CAPABILITY_AND_PERFORMANCE_ROADMAP.md`. It distinguishes work completed
in this repository from acceptance that requires licensed speech data, physical
hardware, a clean Windows install session, elapsed wall-clock time, or a private
publisher certificate. An external gate is not reported as a product pass.

## Status legend

- **Complete:** implemented and covered by an automated contract, regression,
  evaluation, UI, or build check.
- **Ready for external run:** implementation and harness are complete, but the
  stated result requires an external asset or environment.
- **Deferred by decision:** lower-priority work was evaluated and intentionally
  excluded because it lacks measured benefit or would broaden the release.
- **Blocked for public release:** the public-release claim must remain closed.

## Source and evaluation foundation

| Item | Status | Completion evidence |
| --- | --- | --- |
| STAB-01 visualization scope | Complete | Typed chart, flowchart, timeline, and table artifacts, local rendering, text equivalents, tests, and acceptance documentation. |
| STAB-02 static cleanup | Complete | Ruff format/check are required locally, in CI, and in `build.ps1`. |
| STAB-03 documentation consistency | Complete | Schema/version consistency script and tests; feature/status documents updated with this execution. |
| STAB-04 CI | Complete | Windows lint, types, docs, dependency audit, deterministic evaluation, regression, learning, UI, coverage, and package-smoke jobs. |
| STAB-05 local gate | Complete | Local pre-commit hooks reproduce format, lint, and documentation checks. |
| STAB-06 inventories | Complete | CycloneDX SBOM and release manifest scripts record dependencies, licenses, source identity, artifact sizes, and SHA-256 hashes. |
| STAB-07 traceability | Complete for candidate builds | `codex/complete-assistant-roadmap`, versioned artifact names, exact commit plus dirty/source-tree identity, and `SHA256SUMS`; a public tag is created only from a clean approved release commit. |
| EVAL-01 production planner path | Complete | The harness calls the production planner with its schemas, retrieval, examples, candidate skills, and repair behavior. |
| EVAL-02 stable scoring | Complete | Deterministic, rule, structured-argument, clarification, and safety scorers return reason codes. |
| EVAL-03 reports | Complete | JSON and Markdown report task success, categories, failures, latency, retries, and safety. |
| EVAL-04 repeated runs | Complete | Configurable repeats and confidence intervals are included. |
| EVAL-05 category thresholds | Complete | CI evaluates the versioned 100-case deterministic corpus and enforces its thresholds. |
| EVAL-06 sanitized failures | Complete | Reports retain corpus IDs and safe reason codes, not private prompt/response content. |
| Intelligence corpus | Complete | `benchmarks/corpora/assistant_intelligence_v1.json` contains 100 labeled cases across commands, paraphrases, ambiguity, references, retrieval, planning, injection, degradation, and unsupported requests. |
| Speech corpus contract | Complete | Versioned metadata, consent/license fields, audio validation, digests, WER, RTF, false activation, conditions, and threshold reporting. |
| Speech audio assets | Ready for external run | `speech_v1.template.json` covers the required conditions; `.benchmark-data/speech-corpus-validation.json` records the exact missing licensed WAV assets rather than fabricating results. |

## Observability, latency, and voice

| Item | Status | Completion evidence |
| --- | --- | --- |
| OBS-01 typed stages/dimensions | Complete | Typed stage, duration-class, interaction-mode, request-path, outcome, warm/cold, provider/model, and device dimensions. |
| OBS-02 correlated pipeline | Complete | Activation through STT, routing/retrieval/context/planning, policy/execution, response, and TTS share a correlation ID. |
| OBS-03 wait separation | Complete | Compute, provider, interactive wait, and playback durations remain separate. |
| OBS-04 summaries | Complete | Dashboard/benchmark p50, p95, count, error, timeout, and cancellation summaries use shared definitions. |
| OBS-05 resources | Complete | Working set, optional GPU/model data, queue depth, and residency dimensions are available to sustained runs. |
| OBS-06 metric privacy | Complete | Tests reject prompt, transcript, path, credential, and URL-query content in metric labels/exports. |
| STT-01 model benchmark | Ready for external run | CPU/GPU cold load, RAM/VRAM, partial/final latency, WER, RTF, silence, and condition reports are implemented; licensed audio/model selection remains external. |
| STT-02 incremental partials | Complete | Bounded rolling partial windows prevent full-utterance quadratic re-decode. |
| STT-03 partial/final profiles | Complete | Fast partial and accuracy-oriented final profiles are separate. |
| STT-04 warmup/load state | Complete | Optional warmup and cold/warm benchmark fields are implemented. |
| STT-05 native hard stop | Provider-limited | Assistant cancellation stops capture, ignores late work, and releases owned resources; faster-whisper does not expose a safe native in-flight decode abort. |
| STT-06 VAD/echo calibration | Ready for external run | Controls and scoring exist; final thresholds require the physical corpus run. |
| LLM-01 model selection | Ready for external run | Real planner benchmark supports model comparison; only the locally installed model can be measured in this workspace. |
| LLM-02 request budgets | Complete | Candidate, context, prompt, and output budgets vary by request class and are bounded. |
| LLM-03 residency policy | Complete | Configurable preload and keep-alive with cold/warm/resource metrics. |
| LLM-04 hard stop | Provider-limited | HTTP/client tasks cancel and late results are discarded; an Ollama server-side request-stop API is not assumed. |
| LLM-05 split routing model | Deferred by decision | The deterministic router already supplies the lightweight path; no measured benefit justifies another model. |
| TTS-01 real first audio | Ready for external run | Cold/warm multi-phrase benchmark exists; a configured Piper voice and audible hardware run are required. |
| TTS-02 sentence streaming | Complete | Piper synthesis begins by bounded sentence chunks. |
| TTS-03 bounded cache | Complete | Content-addressed phrase cache, atomic temporary files, retention, and privacy tests. |
| TTS-04 barge-in/devices/echo | Ready for external run | Coordination and cancellation tests pass; audible output and physical echo require a person/hardware. |
| TTS-05 pronunciation UI | Deferred by decision | P2; aliases normalize commands, but a voice-specific pronunciation editor is not release-critical. |

## Intelligence, retrieval, and grounding

| Item | Status | Completion evidence |
| --- | --- | --- |
| RET-01 relevance tests | Complete | Labeled namespace/search tests cover relevant, irrelevant, empty, and low-score results. |
| RET-02 embedding default | Ready for external run | Lexical stays the safe default and Ollama embeddings are optional; hardware comparison selects any future default. |
| RET-03 incremental signatures | Complete | Persisted content/provider signatures avoid unchanged rebuilds and detect modification/deletion. |
| RET-04 off-foreground refresh | Complete | Batched indexing and background startup warmup with locking. |
| RET-05 score thresholds | Complete | Namespace-specific floors and empty behavior. |
| RET-06 vector backend | Deferred by scale gate | SQLite/Python scoring remains bounded at the documented local scale; replacement is triggered by benchmark evidence. |
| RET-07 provenance | Complete | Developer traces expose safe namespace/source/score data without private text. |
| PLAN-01 paraphrases | Complete | The 100-case corpus groups canonical goals and varied paraphrases. |
| PLAN-02 ambiguity/referents | Complete | Generic clarification contracts, selected-result state, file/media references, and planner examples. |
| PLAN-03 answer/inspect/act/unsupported | Complete | Grounding and capability-boundary tests prevent false tool use and promises. |
| PLAN-04 replanning/completion | Complete | Frozen workflow stages, typed observations, repeat detection, synthesis, and loop bounds. |
| PLAN-05 repair/timeout | Complete | Bounded repair and explicit invalid/unavailable/timeout outcomes. |
| PLAN-06 critique model | Deferred by decision | No independent measured gain justifies a second completion and its latency. |
| GROUND-01 citations/UI | Complete | Additive web, local-knowledge, local-document, and skill-result citations; display plus inspect/copy controls. |
| GROUND-02 citation coverage | Complete | Retrieved factual answer tests require source linkage. |
| GROUND-03 replanning linkage | Complete | Final synthesis cites the typed observations used. |
| GROUND-04 stale/conflict behavior | Complete | Time/provenance and conflict guidance preserve uncertainty rather than silently merge. |
| GROUND-05 citation privacy | Complete | Credentials/query strings and hidden sensitive document citations are suppressed. |

## Capability, experience, and security

| Area | Status | Completion evidence |
| --- | --- | --- |
| Documents | Complete | Bounded TXT/CSV/PDF/DOCX/XLSX/PPTX extraction, summaries, comparison, action items, local Q&A, locators, persistent incremental indexing, separate read/index grants, citations, cancellation, and corrupt/encrypted/size errors. |
| Visual understanding | Complete for explicit images | OCR, description, chart/error explanation, image/region/text bounds, optional local vision provider, no capture/retention/input authority, and accessible text output. Live screen capture/overlays remain intentionally excluded. |
| Productivity writes | Complete for the approved slice | Exact Drive/OneDrive download/upload, Outlook draft/send and calendar changes, Slack post, Notion create/append, GitHub issue/comment; reviewed scopes, non-retryable mutations, exact previews, confirmation, redaction, examples, and adapter tests. Broader bulk/code/share/delete operations remain excluded. |
| Event automations | Complete for local/file/process/device/web plus typed connector ingestion | Exact file, process, connected-device, and public-HTTPS pollers emit typed hash/metadata observations; calendar and other connectors can submit typed observations but need a configured account-specific source. No event payload becomes a prompt. |
| Creative/lifestyle | Deferred by decision | P2/P3 expansion is not allowed to outrun source quality, network/privacy, and consequential-action reviews. |
| Guided setup | Complete in UI | Resumable ten-step state machine covers privacy, hardware, models, audio, activation, permissions, typed/voice tests, Emergency Stop, diagnostics, skip/failure, and restart persistence. |
| UX-01 recovery | Complete | Categorized audio, CUDA, provider, and database recovery actions in Diagnostics. |
| UX-02 state transparency | Complete | Visible interpretation/tool/action/result traces, exact permission/confirmation previews, status, progress, and response types. |
| UX-03 outcome categories | Complete | Unsupported, unavailable, denied, timed-out, and failed outcomes have stable guidance. |
| UX-04 sources | Complete | Current response sources can be inspected or copied. |
| UX-05 progress | Complete | Pipeline, partial STT, elapsed stage, model warmup, download, execution, dashboard, and automation progress are visible where work is measurable. |
| Accessibility | Complete in automated/source scope | Keyboard shortcuts/focus, accessible labels, high contrast, 100–175% text, reduced motion, non-color status text, captions/text equivalents, and offscreen tests. Packaged assistive-technology acceptance is external. |
| SEC-01 hostile-code decision | Complete | Arbitrary downloaded Python/native code is rejected by default; an OS sandbox is not falsely claimed. |
| SEC-02 execution classes | Complete | Core in-process, first-party managed in-process, explicitly reviewed worker, and blocked-native classes are enforced. |
| SEC-03 brokered authority | Complete for shipped extensions | First-party providers use narrow host adapters; downloaded code has no production execution path by default. |
| SEC-04 hostile tests | Complete for shipping design | Traversal, symlink/package bounds, signature tamper/revocation, secret inheritance, network denial, timeout, process count, capability drift, and default-code rejection. |
| SEC-05 vulnerability scan | Complete | `scripts/audit_dependencies.py` resolves the isolated production dependency graph and runs strict `pip-audit`; unrelated global packages and the non-PyPI editable root cannot mask release results. |
| SEC-06 signing/revocation | Complete | Protected-key and incident runbook, strict Ed25519 metadata, configured revocation IDs, and verification on every load. |

## Maintainability and release

| Item | Status | Completion evidence |
| --- | --- | --- |
| ARCH-01 boundaries | Complete | AST dependency tests and stable public import assertions. |
| ARCH-02 planner split | Complete increment | Planner contracts moved to `language.planner_contracts`; existing application knowledge/understanding/retrieval modules retain policy separation and all planner tests remain green. |
| ARCH-03 orchestrator split | Complete increment | Conversation/replanning state, planner coordination, interaction, file resolution, results, and voice gate live outside the facade; continuation/cancellation tests pass. |
| ARCH-04 desktop split | Complete increment | Onboarding, dashboards, connection pages, notifications, media, visual results, workers, state, tabs, and controllers are modules with UI compatibility tests. A risky wholesale rewrite was not performed. |
| ARCH-05 types | Complete | Selected evaluation, observability, onboarding, planner contract, orchestration state, policy, and scheduling modules pass mypy in CI. |
| ARCH-06 coverage | Complete | Combined regression/learning/UI branch coverage produces JSON and enforces overall plus policy/execution/scheduling aggregate thresholds. |
| ARCH-07 properties | Complete | Generated normalization, command, recurrence, URL/event, and contract invariants. |
| REL-01 candidate artifacts | Complete for internal candidate | Current 243.3 MiB executable and Inno Setup installer build successfully; packaged media verification and isolated calculation smoke pass. Exact source identity records the intentionally dirty roadmap worktree, so a clean commit/tag is still required for public release. |
| REL-02 physical voice/device | Blocked for public release | Requires signed human/hardware run. |
| REL-03 packaged scheduler/toasts | Blocked for public release | Requires installed startup, notification activation, sleep/restart, and live background acceptance. |
| REL-04 upgrade/uninstall | Ready for external run | Migration/uninstall code and automated tests exist; supported Windows account matrix remains manual. |
| REL-05 Authenticode | Blocked for public release | No publisher certificate/private key is available in this workspace. |
| REL-06 24-hour soak | Blocked for public release | Short simulated/sustained tests cannot substitute for elapsed real time. |
| REL-07 SBOM/manifest | Complete | CycloneDX component/license inventory, artifact/source hashes, gate states, and `SHA256SUMS`. |

## Required external acceptance handoff

| Gate | Required input/environment | Command or record | Pass condition |
| --- | --- | --- | --- |
| Speech/model selection | Licensed WAV corpus at template paths; target CPU/GPU | `python scripts/benchmark_models.py --audio-corpus benchmarks/corpora/speech_v1.template.json --audio-assets-root ROOT --validate-speech-corpus` then rerun with the selected `--stt-model` | WER/RTF/latency thresholds pass by condition and sensitive targets never execute unsafely. |
| Piper/audio | Configured `piper.exe`, ONNX voice/config, microphone, speaker | Same benchmark plus `ACCEPTANCE_WORKFLOWS.md` voice lane | Cold/warm first audio, echo, barge-in, and device switching pass. |
| Installed Windows lifecycle | Clean non-admin account and current installer | Package, upgrade, uninstall, startup, toast, sleep/restart lanes | Preservation/removal choices and notification/scheduler behavior match the matrix. |
| Accessibility | Windows scaling and assistive technology | Keyboard/screen-reader/high-contrast acceptance log | Primary setup, command, permission, confirmation, and recovery flows are operable. |
| Signing | Protected Authenticode certificate | `RELEASE_AND_SIGNING_RUNBOOK.md` | Windows verifies publisher for executable and installer; post-sign hashes/manifests match. |
| Soak | Target hardware for 24 elapsed hours | Soak lane in `ACCEPTANCE_WORKFLOWS.md` | No unbounded memory, queue, cache, or database growth; cancellation remains responsive. |

Until those rows pass, the artifacts are internal release candidates—not a
signed public release—even though the repository work in this plan is complete.
