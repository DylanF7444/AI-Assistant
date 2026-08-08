from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import time
import urllib.error
from collections import deque
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from assistant.audio.activation import is_explicit_cancel_command
from assistant.context.providers import (
    ContextProvider,
    PermissionContextProvider,
    PreferenceContextProvider,
    SessionContextProvider,
    SkillContextProvider,
    collect_context_snapshot,
)
from assistant.context.snapshot import ContextSnapshot
from assistant.contracts import (
    ActivityTrace,
    AudioSession,
    CitationSourceType,
    ExecutionPlan,
    Intent,
    PermissionContext,
    PipelineEvent,
    PipelineState,
    PlanStep,
    RequestResultItem,
    Response,
    ResponseCitation,
    ResponseType,
    SkillCall,
    SkillResult,
    TracePhase,
    Transcript,
    WordTimestamp,
)
from assistant.events import EventBus
from assistant.execution.engine import ExecutionEngine, ExecutionOutcome
from assistant.language.planner import (
    InvalidPlannerResponse,
    LocalPlanner,
    PlannedIntentStep,
    PlannerDecision,
)
from assistant.language.retrieval import (
    KnowledgeSemanticIndexer,
    MemorySemanticIndexer,
    RetrievedContextItem,
    SemanticRetriever,
    SkillSemanticIndexer,
    WorkflowTemplateSemanticIndexer,
    playbook_context_item,
)
from assistant.observability import (
    InteractionMode,
    MetricDimensions,
    MetricStage,
    RequestPath,
    process_working_set_bytes,
)
from assistant.orchestration import (
    ConversationState,
    FileReferenceResolver,
    InteractionRecorder,
    PendingClarificationFollowUp,
    PlannerCoordinator,
    ReplanningContinuation,
    contextual_result_selector,
    evaluate_voice_gate,
    select_result_item,
    selection_index,
)
from assistant.policy.permissions import GrantType
from assistant.presentation.media_resolver import is_explicit_media_path
from assistant.presentation.remote_media import is_explicit_remote_media_url
from assistant.presentation.session import MediaSessionRegistry
from assistant.providers import STTProvider, TTSProvider
from assistant.response import response_from_result
from assistant.router.deterministic import DeterministicRouter
from assistant.storage.database import AuditStore, MetricsStore
from assistant.storage.history import HistoryStore
from assistant.storage.knowledge import KnowledgeBaseStore
from assistant.storage.memory import MemoryStore
from assistant.storage.playbooks import Playbook, PlaybookStore
from assistant.windows.paths import (
    ASSISTANT_FOLDER_TOKEN,
    KNOWN_USER_FOLDERS,
    resolve_user_path,
)

logger = logging.getLogger("assistant.orchestrator")


class AssistantOrchestrator:
    def __init__(
        self,
        router: DeterministicRouter,
        engine: ExecutionEngine,
        events: EventBus,
        history: HistoryStore,
        planner: LocalPlanner | None = None,
        tts: TTSProvider | None = None,
        speech_mode: str = "voice_only",
        metrics: MetricsStore | None = None,
        response_detail: str = "normal",
        show_reasoning_steps: bool = False,
        terminal_debug: bool = True,
        allow_sensitive_speech: bool = False,
        hide_sensitive_display: bool = False,
        semantic_retriever: SemanticRetriever | None = None,
        skill_semantic_indexer: SkillSemanticIndexer | None = None,
        knowledge_bases: KnowledgeBaseStore | None = None,
        knowledge_semantic_indexer: KnowledgeSemanticIndexer | None = None,
        memory_store: MemoryStore | None = None,
        memory_semantic_indexer: MemorySemanticIndexer | None = None,
        workflow_template_indexer: WorkflowTemplateSemanticIndexer | None = None,
        playbook_store: PlaybookStore | None = None,
        context_providers: tuple[ContextProvider, ...] = (),
        media_sessions: MediaSessionRegistry | None = None,
    ) -> None:
        self.router = router
        self.engine = engine
        self.events = events
        self.history = history
        self.planner = planner
        self.tts = tts
        self.speech_mode = speech_mode
        self.metrics = metrics
        self.response_detail = response_detail
        self.show_reasoning_steps = show_reasoning_steps
        self.terminal_debug = terminal_debug
        self.allow_sensitive_speech = allow_sensitive_speech
        self.hide_sensitive_display = hide_sensitive_display
        self.semantic_retriever = semantic_retriever
        self.skill_semantic_indexer = skill_semantic_indexer
        self.knowledge_bases = knowledge_bases
        self.knowledge_semantic_indexer = knowledge_semantic_indexer
        self.memory_store = memory_store
        self.memory_semantic_indexer = memory_semantic_indexer
        self.workflow_template_indexer = workflow_template_indexer
        self.playbook_store = playbook_store
        self.context_providers = context_providers
        self.media_sessions = media_sessions or MediaSessionRegistry()
        self._active_correlation: UUID | None = None
        self._request_state_lock = threading.Lock()
        self._active_request_correlations: list[UUID] = []
        self._voice_interaction_starts: dict[UUID, float] = {}
        self._metric_request_starts: dict[UUID, float] = {}
        self._response_visible_recorded: set[UUID] = set()
        self._retrieval_refresh_lock = threading.Lock()
        self._active_stt: STTProvider | None = None
        self.state = ConversationState()
        self.interactions = InteractionRecorder(history, self.state)
        self.file_resolver = FileReferenceResolver(self._resolve_file_placeholders)
        self.planner_flow = PlannerCoordinator()
        self._pending_confirmation: ExecutionOutcome | None = None
        self._replanning_continuations: dict[UUID, ReplanningContinuation] = {}

    @property
    def terminal_debug(self) -> bool:
        return self._terminal_debug

    @terminal_debug.setter
    def terminal_debug(self, enabled: bool) -> None:
        self._terminal_debug = enabled
        provider = getattr(self.planner, "provider", None)
        if provider is not None and hasattr(provider, "terminal_debug"):
            provider.terminal_debug = enabled

    @property
    def _session_context(self) -> deque[dict[str, str]]:
        return self.state.session_context

    @property
    def _file_candidates(self) -> tuple[str, ...]:
        return self.state.file_candidates

    @_file_candidates.setter
    def _file_candidates(self, value: tuple[str, ...]) -> None:
        self.state.file_candidates = value

    @property
    def _selected_file(self) -> str | None:
        return self.state.selected_file

    @_selected_file.setter
    def _selected_file(self, value: str | None) -> None:
        self.state.selected_file = value

    @property
    def _pending_clarification_follow_up(self) -> PendingClarificationFollowUp | None:
        return self.state.pending_clarification

    @_pending_clarification_follow_up.setter
    def _pending_clarification_follow_up(
        self,
        value: PendingClarificationFollowUp | None,
    ) -> None:
        self.state.pending_clarification = value

    def remember_file_candidates(self, paths: tuple[str, ...] | list[str]) -> None:
        self.state.remember_file_candidates(paths)

    @property
    def _result_items(self) -> tuple[RequestResultItem, ...]:
        return self.state.result_items

    @property
    def _selected_result_item(self) -> RequestResultItem | None:
        return self.state.selected_result_item

    def remember_result_items(
        self,
        items: tuple[RequestResultItem, ...] | list[RequestResultItem],
        *,
        source: str | None = None,
        summary: str | None = None,
    ) -> None:
        self.state.remember_result_items(items, source=source, summary=summary)

    def remember_result(
        self,
        result: SkillResult,
        *,
        source: str | None = None,
    ) -> None:
        self._remember_result(result, source=source)

    @property
    def request_in_progress(self) -> bool:
        with self._request_state_lock:
            return bool(self._active_request_correlations)

    def discard_voice_interaction(self, correlation_id: UUID) -> None:
        """Release timing state when passive audio was not an assistant request."""

        self._voice_interaction_starts.pop(correlation_id, None)

    async def consume_persistent_voice_interrupt(self, text: str) -> bool:
        """Consume speech heard during a request, cancelling only on an explicit command."""

        if not self.request_in_progress:
            return False
        if is_explicit_cancel_command(text):
            await self.cancel()
        return True

    def _remember_interaction(
        self,
        correlation_id: UUID,
        request_text: str,
        response: Response,
        *,
        intent_name: str,
        outcome: str,
        add_session_context: bool = True,
    ) -> None:
        self.interactions.record(
            correlation_id,
            request_text,
            response,
            intent_name=intent_name,
            outcome=outcome,
            add_session_context=add_session_context,
        )

    async def _finish_interaction(
        self,
        correlation_id: UUID,
        request_text: str,
        response: Response,
        outcome: ExecutionOutcome | None = None,
        *,
        intent_name: str,
        outcome_label: str,
        invoked_by_voice: bool,
        add_session_context: bool = True,
    ) -> tuple[Response, ExecutionOutcome]:
        final_outcome = outcome or ExecutionOutcome()
        if final_outcome.confirmation is not None:
            self._pending_confirmation = final_outcome
        if final_outcome.result:
            self._remember_result(
                final_outcome.result,
                source=(
                    final_outcome.call.skill_id if final_outcome.call is not None else intent_name
                ),
            )
        self._print_terminal_debug(
            correlation_id,
            request_text,
            response,
            final_outcome,
            intent_name=intent_name,
            outcome_label=outcome_label,
        )
        self._remember_interaction(
            correlation_id,
            request_text,
            response,
            intent_name=intent_name,
            outcome=outcome_label,
            add_session_context=add_session_context,
        )
        return await self._finish(
            response,
            final_outcome,
            invoked_by_voice=invoked_by_voice,
            correlation_id=correlation_id,
        )

    def _print_terminal_debug(
        self,
        correlation_id: UUID,
        request_text: str,
        response: Response,
        outcome: ExecutionOutcome,
        *,
        intent_name: str,
        outcome_label: str,
    ) -> None:
        if not self.terminal_debug:
            return
        call = outcome.call
        lines = [
            "[assistant debug]",
            f"correlation_id={correlation_id}",
            f"request={AuditStore.redact(request_text)}",
            f"intent={intent_name}",
            f"outcome={outcome_label}",
            f"response_type={response.response_type.value}",
            f"severity={response.severity}",
        ]
        if call is not None:
            lines.extend(
                [
                    f"skill_id={call.skill_id}",
                    f"caller={call.caller}",
                    "arguments="
                    + json.dumps(
                        AuditStore.redact(call.arguments),
                        sort_keys=True,
                        default=str,
                    ),
                ]
            )
        if outcome.permission_requests:
            lines.append(
                "permission_requests="
                + json.dumps(
                    [
                        {
                            "skill_id": request.call.skill_id,
                            **request.scope.model_dump(),
                        }
                        for request in outcome.permission_requests
                    ],
                    sort_keys=True,
                    default=str,
                )
            )
        if outcome.registration_requests:
            lines.append(
                "registration_requests="
                + json.dumps(
                    [
                        {
                            "skill_id": request.call.skill_id,
                            "application": request.application,
                        }
                        for request in outcome.registration_requests
                    ],
                    sort_keys=True,
                    default=str,
                )
            )
        if outcome.confirmation:
            lines.append(
                "confirmation="
                + json.dumps(
                    outcome.confirmation.model_dump(mode="json"),
                    sort_keys=True,
                    default=str,
                )
            )
        if outcome.result:
            lines.extend(
                [
                    f"result_status={outcome.result.status.value}",
                    f"result_summary={outcome.result.summary}",
                ]
            )
        print("\n".join(lines), file=sys.stderr, flush=True)

    async def _record_routing(
        self,
        correlation_id: UUID,
        intent: Intent | None,
        duration_ms: float,
        *,
        interaction: InteractionMode,
        path: RequestPath,
    ) -> None:
        if self.metrics:
            self.metrics.record(
                correlation_id=str(correlation_id),
                stage=MetricStage.ROUTING,
                duration_ms=duration_ms,
                value=intent.confidence if intent else None,
                outcome=intent.name if intent else "clarification",
                dimensions=MetricDimensions(interaction=interaction, path=path),
            )
        self.engine.audit.record(
            correlation_id=str(correlation_id),
            event_type="routing",
            decision=intent.name if intent else "clarification",
            details={
                "confidence": intent.confidence if intent else None,
                "source": intent.source if intent else None,
                "ambiguous": intent.ambiguous if intent else True,
            },
        )
        if intent is None:
            detail = "No deterministic action matched; clarification is required."
        else:
            detail = (
                f"Selected {intent.name} via {intent.source} ({intent.confidence:.0%} confidence)."
            )
        await self.events.publish(
            PipelineEvent(
                correlation_id=correlation_id,
                state=PipelineState.THINKING,
                message=detail,
                trace=ActivityTrace(
                    phase=TracePhase.DECISION,
                    label="Decision",
                    detail=detail,
                ),
                updates_foreground_status=False,
            )
        )

    @staticmethod
    def _planner_unavailable_response() -> Response:
        return Response(
            display_text="I could not match that to an offline core command.",
            speech_text="I could not match that to an available command.",
            response_type=ResponseType.ERROR,
            severity="warning",
        )

    @staticmethod
    def _planner_failed_response(exc: Exception) -> Response:
        unavailable = isinstance(
            exc,
            (
                ConnectionError,
                urllib.error.URLError,
            ),
        )
        guidance = (
            " Start Ollama and verify the selected local model is installed."
            if unavailable
            else " The selected local model did not respond before the planner "
            "timeout. Try again, or choose a faster installed local model."
            if isinstance(exc, TimeoutError)
            else " The local model returned an invalid planner response. Please try again."
        )
        debug = ""
        if isinstance(exc, InvalidPlannerResponse):
            raw_response = json.dumps(
                exc.invalid_response,
                indent=2,
                sort_keys=True,
                default=str,
            )
            if len(raw_response) > 4000:
                raw_response = raw_response[:4000] + "\n... truncated ..."
            debug = (
                "\n\nDebug invalid planner response:\n"
                f"{raw_response}\n"
                f"Validation error: {exc.message}"
            )
        return Response(
            display_text=(
                "I could not use the local planner."
                f"{guidance} Deterministic commands still work."
                f"{debug}"
            ),
            speech_text="The local planner is unavailable.",
            response_type=ResponseType.ERROR,
            severity="warning",
        )

    def _response_from_outcome(self, outcome: ExecutionOutcome) -> Response:
        if outcome.registration_requests and outcome.permission_requests:
            registrations = [
                f"- {request.application}: choose the application executable to register it."
                for request in outcome.registration_requests
            ]
            permissions = []
            for request in outcome.permission_requests:
                target_text = (
                    f" for {', '.join(request.scope.targets)}" if request.scope.targets else ""
                )
                permissions.append(
                    f"- {request.scope.capability}{target_text}: {request.scope.reason}"
                )
            return Response(
                display_text=(
                    "I need a registration and permission before I can run this plan:\n"
                    + "\n".join(registrations)
                    + "\n"
                    + "\n".join(permissions)
                    + "\n\nRegister the application first, then review the permission request."
                ),
                speech_text="I need an application registration and permission. Please review the request on screen.",
                response_type=ResponseType.PERMISSION,
                severity="warning",
                speak=True,
            )
        if outcome.permission_requests:
            descriptions = []
            for request in outcome.permission_requests:
                target_text = (
                    f" for {', '.join(request.scope.targets)}" if request.scope.targets else ""
                )
                descriptions.append(
                    f"- {request.scope.capability}{target_text}: {request.scope.reason}"
                )
            return Response(
                display_text=(
                    "I need your permission before I can do that:\n"
                    + "\n".join(descriptions)
                    + "\n\nChoose Grant Permission to approve this request."
                ),
                speech_text="I need your permission. Please review the request on screen.",
                response_type=ResponseType.PERMISSION,
                severity="warning",
                speak=True,
            )
        if outcome.registration_requests:
            descriptions = [
                f"- {request.application}: choose the application executable to register it."
                for request in outcome.registration_requests
            ]
            return Response(
                display_text=(
                    "I need this application registered before I can open it:\n"
                    + "\n".join(descriptions)
                    + "\n\nChoose Register Application to continue."
                ),
                speech_text="I need the application registered. Please review the request on screen.",
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )
        if outcome.confirmation:
            return Response(
                display_text=outcome.confirmation.exact_action,
                speech_text="Please review and confirm the action shown on screen.",
                response_type=ResponseType.CONFIRMATION,
                severity="warning",
                speak=True,
            )
        if outcome.result:
            return response_from_result(
                outcome.result,
                detail=self.response_detail,
                allow_sensitive_speech=self.allow_sensitive_speech,
                hide_sensitive_display=self.hide_sensitive_display,
            )
        return Response(
            display_text="No action was performed.",
            response_type=ResponseType.STATUS,
            severity="warning",
        )

    async def _context_snapshot_for_planner_goal(
        self,
        text: str,
        *,
        session_id: str,
        candidate_skill_ids: tuple[str, ...] | None = None,
    ) -> ContextSnapshot:
        providers = [
            SessionContextProvider(
                recent_context=lambda: tuple(self._session_context),
                pending_clarification=self._pending_clarification_context,
                selected_result_item=lambda: self._selected_result_item,
                result_items=lambda: self._result_items,
                result_source=lambda: self.state.result_source,
                selected_file=lambda: self._selected_file,
                file_candidates=lambda: self._file_candidates,
            ),
            SkillContextProvider(self.engine.registry),
            PermissionContextProvider(
                self.engine.permissions,
                confirmation_overrides=self.engine.confirmation_overrides,
            ),
        ]
        if self.memory_store is not None:
            providers.append(PreferenceContextProvider(self.memory_store))
        providers.extend(self.context_providers)
        return await collect_context_snapshot(
            text,
            session_id=session_id,
            providers=tuple(providers),
            candidate_skill_ids=candidate_skill_ids,
        )

    def _pending_clarification_context(self) -> dict[str, str] | None:
        pending = self._pending_clarification_follow_up
        if pending is None:
            return None
        return {
            "original_text": pending.original_text,
            "clarification": pending.clarification,
            "kind": pending.kind,
        }

    async def _interpret_planner_goal(
        self,
        correlation_id: UUID,
        text: str,
        *,
        session_id: str,
    ) -> PlannerDecision | None:
        assert self.planner is not None
        if composite := self._deterministic_composite_plan(text):
            return composite
        explicit_playbook = self._explicit_playbook_reference(text)
        if explicit_playbook is not None:
            playbook, reference = explicit_playbook
            if playbook is None:
                available = (
                    ", ".join(item.name for item in self.playbook_store.list())
                    if self.playbook_store is not None
                    else ""
                )
                suffix = f" Available playbooks: {available}." if available else ""
                return PlannerDecision(
                    intent=Intent(
                        name="conversation.answer",
                        arguments={
                            "text": (
                                f'I could not find a playbook named or identified by "{reference}".'
                                f"{suffix}"
                            )
                        },
                        confidence=1,
                        source="rule",
                    )
                )
            if not playbook.enabled:
                return PlannerDecision(
                    intent=Intent(
                        name="conversation.answer",
                        arguments={
                            "text": (
                                f'The playbook "{playbook.name}" is disabled. '
                                f'Say "enable playbook {playbook.playbook_id}" before using it.'
                            )
                        },
                        confidence=1,
                        source="rule",
                    )
                )
        retrieval_started = time.perf_counter()
        retrieved = await self._retrieve_planner_goal_context(text)
        if self.metrics is not None:
            self.metrics.record(
                correlation_id=str(correlation_id),
                stage=MetricStage.PLANNER_RETRIEVAL,
                duration_ms=(time.perf_counter() - retrieval_started) * 1000,
                value=len(retrieved),
                outcome="completed",
                dimensions=MetricDimensions(path=RequestPath.PLANNER),
            )
        if explicit_playbook is not None:
            playbook, _ = explicit_playbook
            assert playbook is not None
            exact = playbook_context_item(playbook, self.engine.registry)
            retrieved = (exact,) + tuple(
                item
                for item in retrieved
                if not (item.namespace == "playbook" and item.source_id == playbook.playbook_id)
            )
        candidate_skills = None
        candidate_skill_ids = None
        select_candidates = getattr(self.planner, "candidate_skills", None)
        if callable(select_candidates):
            candidate_skills = select_candidates(text, retrieved)
            select_context_candidates = getattr(
                self.planner,
                "context_candidate_skill_ids",
                None,
            )
            candidate_skill_ids = (
                select_context_candidates(retrieved, candidate_skills)
                if callable(select_context_candidates)
                else tuple(manifest.skill_id for manifest in candidate_skills)
            )
        context_started = time.perf_counter()
        context = await self._context_snapshot_for_planner_goal(
            text,
            session_id=session_id,
            candidate_skill_ids=candidate_skill_ids,
        )
        if self.metrics is not None:
            self.metrics.record(
                correlation_id=str(correlation_id),
                stage=MetricStage.PLANNER_CONTEXT,
                duration_ms=(time.perf_counter() - context_started) * 1000,
                value=len(context.items),
                outcome="completed",
                dimensions=MetricDimensions(path=RequestPath.PLANNER),
            )
        plan_grounded = getattr(self.planner, "plan_grounded", None)
        if callable(plan_grounded):
            grounded_started = time.perf_counter()
            decision = await plan_grounded(
                text,
                context=context,
                retrieved=retrieved,
                candidate_skills=candidate_skills,
            )
            if self.metrics is not None:
                outcome = (
                    decision.intent.name
                    if decision.intent is not None
                    else "plan"
                    if decision.steps
                    else "clarification"
                    if decision.clarification
                    else "empty"
                )
                self.metrics.record(
                    correlation_id=str(correlation_id),
                    stage=MetricStage.PLANNER_GROUNDED,
                    duration_ms=(time.perf_counter() - grounded_started) * 1000,
                    outcome=outcome,
                    dimensions=MetricDimensions(path=RequestPath.PLANNER),
                )
            return decision
        interpretation = await self.planner.interpret(
            text,
            context=context,
            retrieved=retrieved,
        )
        if interpretation.should_ask_clarification:
            question = (interpretation.clarification_question or "").strip()
            if question:
                return PlannerDecision(clarification=question)
        if hasattr(self.planner, "plan_from_interpretation"):
            decision = await self.planner.plan_from_interpretation(
                text,
                interpretation=interpretation,
                context=context,
                retrieved=retrieved,
            )
            return PlannerDecision(
                intent=decision.intent,
                steps=decision.steps,
                clarification=decision.clarification,
                interpretation=interpretation,
                context=context,
                retrieved=retrieved,
                allow_replanning=True,
            )
        return None

    def _deterministic_composite_plan(self, text: str) -> PlannerDecision | None:
        """Resolve independent command conjunctions without spending a model round trip."""

        clauses = tuple(
            clause.strip(" ,")
            for clause in re.split(r"\s+(?:and|then)\s+", text, flags=re.IGNORECASE)
            if clause.strip(" ,")
        )
        if not 2 <= len(clauses) <= 4:
            return None
        shared_verb = (
            match.group(1)
            if (match := re.match(r"\s*(list|show)\b", clauses[0], flags=re.IGNORECASE))
            else None
        )
        intents: list[Intent] = []
        for index, clause in enumerate(clauses):
            if index and re.search(
                r"\b(?:it|that|this|those|them|one|result|match|selected|found)\b",
                clause,
                flags=re.IGNORECASE,
            ):
                return None
            routed_clause = (
                f"{shared_verb} {clause}"
                if index and shared_verb and re.match(r"(?:my|the)\b", clause, re.IGNORECASE)
                else clause
            )
            route = self.router.route(routed_clause)
            if route.needs_llm or route.clarification:
                return None
            routed_intents = route.intents or ((route.intent,) if route.intent is not None else ())
            if not routed_intents or any(
                intent.name.startswith(("planner.", "conversation.", "control."))
                for intent in routed_intents
            ):
                return None
            intents.extend(routed_intents)
        if len(intents) < 2:
            return None
        return PlannerDecision(
            steps=tuple(
                PlannedIntentStep(
                    step_id=f"deterministic-{index}",
                    intent=intent,
                )
                for index, intent in enumerate(intents, start=1)
            )
        )

    def _explicit_playbook_reference(
        self,
        text: str,
    ) -> tuple[Playbook | None, str] | None:
        if self.playbook_store is None:
            return None
        match = re.fullmatch(
            r"\s*(?:use|run|follow|apply)\s+(?:the\s+)?playbook\s+(.+?)\s*[.!]?\s*",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match is None:
            return None
        remainder = match.group(1).strip()
        folded = remainder.casefold()
        candidates = []
        for playbook in self.playbook_store.list():
            for alias in {playbook.playbook_id, playbook.name}:
                candidates.append((alias, playbook))
        for alias, playbook in sorted(
            candidates,
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            folded_alias = alias.casefold()
            if folded == folded_alias:
                return playbook, alias
            if not folded.startswith(folded_alias + " "):
                continue
            tail = remainder[len(alias) :].strip()
            if re.match(r"^(?:to|for|on)\b", tail, flags=re.IGNORECASE):
                return playbook, re.sub(
                    r"^(?:to|for|on)\s+",
                    "",
                    tail,
                    count=1,
                    flags=re.IGNORECASE,
                )
        return None, remainder

    async def _retrieve_planner_goal_context(
        self,
        text: str,
    ) -> tuple[RetrievedContextItem, ...]:
        if self.semantic_retriever is None:
            return ()
        try:
            await self.refresh_retrieval_index()
            return await self.semantic_retriever.search(
                text,
                namespaces={
                    "knowledge",
                    "memory",
                    "playbook",
                    "skill",
                    "skill_example",
                    "workflow_template",
                },
                limit=8,
            )
        except Exception as exc:
            logger.warning("Semantic retrieval failed", exc_info=exc)
            return ()

    async def refresh_retrieval_index(self) -> None:
        """Refresh semantic namespaces, serializing startup and request-triggered work."""

        acquired = await asyncio.to_thread(self._retrieval_refresh_lock.acquire)
        if not acquired:
            return
        try:
            if self.skill_semantic_indexer is not None:
                await self.skill_semantic_indexer.refresh(self.engine.registry)
            if self.knowledge_semantic_indexer is not None and self.knowledge_bases is not None:
                await self.knowledge_semantic_indexer.refresh(self.knowledge_bases)
            if self.memory_semantic_indexer is not None and self.memory_store is not None:
                await self.memory_semantic_indexer.refresh(self.memory_store)
            if self.workflow_template_indexer is not None:
                await self.workflow_template_indexer.refresh(self.engine.registry)
        finally:
            self._retrieval_refresh_lock.release()

    def _with_reasoning_steps(
        self,
        response: Response,
        *,
        request_text: str,
        intent: Intent | None,
        outcome_label: str,
        step_summaries: tuple[str, ...] = (),
        interpretation=None,
        context: ContextSnapshot | None = None,
        retrieved: tuple[Any, ...] = (),
    ) -> Response:
        citations = self._citations_from_retrieved(retrieved)
        if citations:
            existing = {citation.source_id for citation in response.citations}
            response = response.model_copy(
                update={
                    "citations": response.citations
                    + tuple(
                        citation for citation in citations if citation.source_id not in existing
                    )
                }
            )
        if not self.show_reasoning_steps:
            return response
        lines = [
            "",
            "Request breakdown:",
            f"- Request: {request_text}",
        ]
        if interpretation is not None:
            lines.extend(
                [
                    f"- User goal: {interpretation.user_goal}",
                    f"- Request type: {interpretation.request_type}",
                ]
            )
            if interpretation.missing_information:
                lines.append(
                    "- Missing information: " + "; ".join(interpretation.missing_information)
                )
            if interpretation.assumptions:
                lines.append("- Assumptions: " + "; ".join(interpretation.assumptions))
            if interpretation.relevant_context_keys:
                lines.append(
                    "- Relevant context keys: " + ", ".join(interpretation.relevant_context_keys)
                )
        if context is not None:
            lines.append("- Context keys: " + (", ".join(context.keys()) or "none"))
        if retrieved:
            lines.append(
                "- Retrieved items: "
                + json.dumps(
                    [self._retrieved_debug_item(item) for item in retrieved],
                    sort_keys=True,
                    default=str,
                )
            )
        if intent is None:
            lines.append("- Classification: clarification needed")
        else:
            lines.extend(
                [
                    (
                        "- Classification: "
                        f"{intent.name} via {intent.source} "
                        f"(confidence {intent.confidence:.0%})"
                    ),
                    f"- Arguments: {json.dumps(intent.arguments, default=str)}",
                ]
            )
            if intent.ambiguous and intent.ambiguity_reasons:
                lines.append("- Ambiguity: " + "; ".join(intent.ambiguity_reasons))
        if step_summaries:
            lines.append("- Steps:")
            lines.extend(f"  {index}. {summary}" for index, summary in enumerate(step_summaries, 1))
        else:
            lines.append(f"- Step: {outcome_label.replace('_', ' ')}")
        lines.append(f"- Result state: {outcome_label.replace('_', ' ')}")
        return response.model_copy(
            update={"display_text": response.display_text + "\n".join(lines)}
        )

    @staticmethod
    def _citations_from_retrieved(
        retrieved: tuple[Any, ...],
    ) -> tuple[ResponseCitation, ...]:
        citations = []
        seen = set()
        for item in retrieved:
            if getattr(item, "namespace", "") != "knowledge":
                continue
            source_id = str(getattr(item, "source_id", "")).strip()
            metadata = getattr(item, "metadata", {})
            if not source_id or source_id in seen or not isinstance(metadata, dict):
                continue
            seen.add(source_id)
            knowledge_id = metadata.get("knowledge_base_id") or source_id.split(":", 1)[0]
            title = str(metadata.get("name") or "Local knowledge")
            chunk = source_id.rsplit(":", 1)[-1]
            citations.append(
                ResponseCitation(
                    source_id=source_id,
                    title=title,
                    source_type=CitationSourceType.LOCAL_KNOWLEDGE,
                    local_reference=f"knowledge:{knowledge_id}",
                    excerpt_locator=(f"chunk {chunk}" if chunk.isdigit() else None),
                    namespace="knowledge",
                    score=float(getattr(item, "score", 0.0)),
                )
            )
        return tuple(citations[:5])

    @staticmethod
    def _retrieved_debug_item(item: Any) -> dict[str, Any]:
        metadata = getattr(item, "metadata", {})
        safe_metadata = {}
        if isinstance(metadata, dict):
            for key in (
                "skill_id",
                "skill_ids",
                "skill_stages",
                "completion_skill_ids",
                "playbook_id",
                "template_id",
                "knowledge_base_id",
                "type",
                "key",
                "requires_clarification",
            ):
                if key in metadata:
                    safe_metadata[key] = metadata[key]
        return {
            "namespace": getattr(item, "namespace", ""),
            "source_id": getattr(item, "source_id", ""),
            "score": round(float(getattr(item, "score", 0.0)), 3),
            "metadata": safe_metadata,
        }

    @staticmethod
    def _outcome_label(outcome: ExecutionOutcome) -> str:
        if outcome.confirmation:
            return "awaiting_confirmation"
        if outcome.permission_requests:
            return "awaiting_permission"
        if outcome.registration_requests:
            return "awaiting_registration"
        if outcome.result:
            return outcome.result.status.value
        return "none"

    def _remember_result(self, result: SkillResult, *, source: str | None = None) -> None:
        if result.items is not None:
            self.remember_result_items(
                result.items,
                source=source,
                summary=result.summary,
            )
        else:
            # Compatibility for installed skills that predate the official item
            # contract and return file-search candidates only in result data.
            paths = result.data.get("paths") if isinstance(result.data, dict) else None
            if (
                isinstance(paths, list)
                and all(isinstance(path, str) for path in paths)
                and (paths or source == "windows.file_search")
            ):
                self.remember_file_candidates(tuple(paths))
        if result.media_directive is not None:
            self.state.remember_media_directive(result.media_directive)

    def _select_result_item(self, selector: str) -> Response:
        selection = select_result_item(self._result_items, selector)
        if selection.item is None:
            if selection.failure == "no_results":
                display = "There are no recent request result items to select from."
                speech = "There are no recent results to select from."
            elif selection.failure == "out_of_range":
                display = (
                    "I do not have that many recent results. Select one of the "
                    'numbered items, for example "select 1".'
                )
                speech = "I do not have that many recent results."
            elif selection.failure == "ambiguous":
                display = (
                    "That name matches more than one recent result. Select it by "
                    'number or item ID, for example "select 1".'
                )
                speech = "That name matches more than one result."
            else:
                display = (
                    "I could not match that reference to a recent result. Select an "
                    'item by number, ID, or title, for example "select 1".'
                )
                speech = "I could not match that recent result."
            return Response(
                display_text=display,
                speech_text=speech,
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )
        item = selection.item
        self.state.select_result_item(item)
        position = self._result_items.index(item) + 1
        title = "[sensitive item]" if item.sensitive else item.title
        return Response(
            display_text=f"Selected item {position}: {title}",
            speech_text="Selected.",
            response_type=ResponseType.STATUS,
            severity="success",
            speak=True,
            sensitive=item.sensitive,
            items=(item,),
            selected_item_id=item.item_id,
        )

    def _select_contextual_result_reference(self, text: str) -> None:
        """Resolve an explicit numbered result reference before planner context is built."""

        selector = contextual_result_selector(text)
        if selector is None:
            return
        selection = select_result_item(self._result_items, selector)
        if selection.item is not None:
            self.state.select_result_item(selection.item)

    def _select_file_candidate(self, selector: str) -> Response:
        """Compatibility alias for callers using the former file-only system."""

        return self._select_result_item(selector)

    @staticmethod
    def _selection_index(selector: str) -> int | None:
        return selection_index(selector)

    def _candidate_reference(self, value: str) -> str | Response | None:
        if not self._file_candidates:
            return None
        cleaned = value.strip().strip("\"'").casefold()
        if not cleaned:
            return None
        index = self._selection_index(cleaned)
        if index == -1:
            index = len(self._file_candidates) - 1
        if index is not None:
            if 0 <= index < len(self._file_candidates):
                return self._file_candidates[index]
            return Response(
                display_text=(
                    "I do not have that many file search results. Choose one of "
                    'the numbered results, for example "the first one".'
                ),
                speech_text="I do not have that many file results.",
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )

        named_reference = self._file_candidate_name_reference(cleaned)
        if not named_reference:
            return None
        location_reference = ""
        if match := re.fullmatch(
            r"(?P<name>.+?)\s+(?:in|inside|on|from)\s+(?P<location>.+)",
            named_reference,
        ):
            named_reference = self._file_candidate_name_reference(match.group("name"))
            location_reference = match.group("location").strip()
        if not named_reference:
            return None

        scored_matches = sorted(
            (
                (score, path)
                for path in self._file_candidates
                if self._file_candidate_matches_location(path, location_reference)
                for score in [self._file_candidate_name_score(named_reference, path)]
                if score is not None
            ),
            key=lambda item: item[0],
        )
        if len(scored_matches) == 1:
            return scored_matches[0][1]
        if scored_matches:
            best_score = scored_matches[0][0]
            best_matches = [path for score, path in scored_matches if score == best_score]
            if len(best_matches) == 1:
                return best_matches[0]
            return Response(
                display_text=(
                    "That file reference matches more than one result. Use the "
                    'numbered result instead, for example "the first one".'
                ),
                speech_text="That matches more than one file result.",
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )
        return None

    @staticmethod
    def _file_candidate_name_reference(value: str) -> str:
        named_reference = re.sub(
            r"^(?:the|that|this|my)\s+", "", value.strip(), flags=re.IGNORECASE
        )
        named_reference = re.sub(
            r"\s+(?:one|file|item|result|match)$",
            "",
            named_reference,
            flags=re.IGNORECASE,
        ).strip()
        return named_reference

    @staticmethod
    def _file_candidate_matches_location(path: str, location_reference: str) -> bool:
        if not location_reference:
            return True
        normalized = AssistantOrchestrator._normalize_user_folder_reference(location_reference)
        first = normalized.replace("\\", "/").partition("/")[0].casefold()
        path_obj = Path(path)
        folded_parts = {part.casefold() for part in path_obj.parts}
        if first in KNOWN_USER_FOLDERS:
            return KNOWN_USER_FOLDERS[first].casefold() in folded_parts
        words = [
            word
            for word in re.split(
                r"[^a-z0-9]+",
                re.sub(
                    r"^(?:the|my)\s+",
                    "",
                    location_reference.strip().casefold(),
                    flags=re.IGNORECASE,
                ),
            )
            if word and word != "folder"
        ]
        parent = str(path_obj.parent).casefold()
        return bool(words) and all(word in parent for word in words)

    @staticmethod
    def _file_candidate_name_score(reference: str, path: str) -> int | None:
        query = AssistantOrchestrator._file_candidate_name_reference(reference).casefold()
        if not query:
            return None
        candidate = Path(path)
        name = candidate.name.casefold()
        stem = candidate.stem.casefold()
        if query in {name, stem}:
            return 0
        query_stem = Path(query).stem.casefold()
        if query_stem and query_stem in {name, stem}:
            return 1
        words = [word for word in re.split(r"[^a-z0-9]+", query_stem or query) if word]
        if words and all(word in name for word in words):
            return 2
        return None

    @staticmethod
    def _normalize_user_folder_reference(value: str) -> str:
        candidate = value.strip()
        trimmed = re.sub(r"^(?:my|the)\s+", "", candidate, flags=re.IGNORECASE)
        trimmed = re.sub(r"\s+folder$", "", trimmed, flags=re.IGNORECASE)
        first = trimmed.replace("\\", "/").partition("/")[0].casefold()
        if first in KNOWN_USER_FOLDERS:
            return trimmed
        return candidate

    @staticmethod
    def _destination_is_folder_reference(original: str, resolved: Path) -> bool:
        normalized = AssistantOrchestrator._normalize_user_folder_reference(original)
        first = normalized.replace("\\", "/").partition("/")[0].casefold()
        if first in KNOWN_USER_FOLDERS and not Path(normalized).suffix:
            return True
        if re.search(r"\bfolder$", original.strip(), flags=re.IGNORECASE):
            return True
        return resolved.exists() and resolved.is_dir()

    def _resolve_file_placeholders(
        self, intent: Intent, *, session_id: str, request_text: str
    ) -> Intent | Response:
        if intent.name == "media.viewer_control":
            if self.media_sessions.active_presentation_id() is None:
                return Response(
                    display_text="There is no active in-app media viewer to control.",
                    speech_text="There is no active media viewer.",
                    response_type=ResponseType.CLARIFICATION,
                    severity="warning",
                    speak=True,
                )
            return intent
        if intent.name == "media.present":
            return self._resolve_media_placeholders(
                intent,
                session_id=session_id,
                request_text=request_text,
            )
        if intent.name != "windows.file_operation":
            return intent
        arguments = dict(intent.arguments)
        if arguments.get("source") == "${selected_file}":
            if not self._selected_file:
                return Response(
                    display_text=("Select one of the file search results before using that file."),
                    speech_text="Select a file first.",
                    response_type=ResponseType.CLARIFICATION,
                    severity="warning",
                    speak=True,
                )
            arguments["source"] = self._selected_file
        elif isinstance(arguments.get("source"), str):
            source_reference = self._candidate_reference(arguments["source"])
            if isinstance(source_reference, Response):
                return source_reference
            if source_reference is not None:
                arguments["source"] = source_reference
                self._selected_file = source_reference
        destination = arguments.get("destination")
        if isinstance(destination, str):
            if "${selected_file_name}" in destination:
                selected_name = Path(arguments["source"]).name
                destination = destination.replace("${selected_file_name}", selected_name)
            resolved_destination = self._resolve_named_folder_path(
                destination,
                session_id=session_id,
                request_text=request_text,
            )
            if isinstance(resolved_destination, Response):
                return resolved_destination
            destination = resolved_destination
            destination = self._normalize_user_folder_reference(destination)
            try:
                destination_path = resolve_user_path(destination)
            except ValueError:
                arguments["destination"] = destination
            else:
                operation = arguments.get("operation")
                source = arguments.get("source")
                if (
                    operation in {"copy", "move"}
                    and isinstance(source, str)
                    and self._destination_is_folder_reference(destination, destination_path)
                ):
                    destination_path = destination_path / Path(source).name
                arguments["destination"] = str(destination_path)
        if "source" in arguments:
            source = arguments["source"]
            if isinstance(source, str):
                resolved_source = self._resolve_named_folder_path(
                    source,
                    session_id=session_id,
                    request_text=request_text,
                )
                if isinstance(resolved_source, Response):
                    return resolved_source
                source = resolved_source
                source = self._normalize_user_folder_reference(source)
                if self._looks_like_unresolved_file_reference(source):
                    return Response(
                        display_text=(
                            f'I could not resolve "{source}" to an exact file. '
                            "Select one of the file search results, for example "
                            '"select 1", or use a full path.'
                        ),
                        speech_text="Please select one of the file results or use a full path.",
                        response_type=ResponseType.CLARIFICATION,
                        severity="warning",
                        speak=True,
                    )
            arguments["source"] = str(resolve_user_path(source))
        return intent.model_copy(update={"arguments": arguments})

    def _resolve_media_placeholders(
        self,
        intent: Intent,
        *,
        session_id: str,
        request_text: str,
    ) -> Intent | Response:
        arguments = dict(intent.arguments)
        source = arguments.get("source")
        if isinstance(source, str):
            if is_explicit_remote_media_url(source):
                return intent
            if source == "${selected_file}" or source.casefold() in {
                "it",
                "that",
                "this",
                "the selected file",
                "the selected item",
            }:
                if not self._selected_file:
                    return Response(
                        display_text=("Select one of the file search results before showing it."),
                        speech_text="Select a file first.",
                        response_type=ResponseType.CLARIFICATION,
                        severity="warning",
                        speak=True,
                    )
                source = self._selected_file
            else:
                candidate = self._candidate_reference(source)
                if isinstance(candidate, Response):
                    return candidate
                if candidate is not None:
                    source = candidate
                    self._selected_file = candidate
            resolved_source = self._resolve_named_folder_path(
                source,
                session_id=session_id,
                request_text=request_text,
            )
            if isinstance(resolved_source, Response):
                return resolved_source
            source = self._normalize_user_folder_reference(resolved_source)
            if not is_explicit_media_path(source):
                return Response(
                    display_text=(
                        f'I could not resolve "{source}" to one exact media file. '
                        "Select a numbered result or name the folder to search."
                    ),
                    speech_text=("Select a media result or tell me which folder to search."),
                    response_type=ResponseType.CLARIFICATION,
                    severity="warning",
                    speak=True,
                )
            try:
                arguments["source"] = str(resolve_user_path(source))
            except ValueError as exc:
                return Response(
                    display_text=f"I could not resolve that media path: {exc}",
                    speech_text="I could not resolve that media path.",
                    response_type=ResponseType.CLARIFICATION,
                    severity="warning",
                    speak=True,
                )
        root = arguments.get("root")
        if isinstance(root, str):
            resolved_root = self._resolve_named_folder_path(
                root,
                session_id=session_id,
                request_text=request_text,
            )
            if isinstance(resolved_root, Response):
                return resolved_root
            try:
                arguments["root"] = str(
                    resolve_user_path(self._normalize_user_folder_reference(resolved_root))
                )
            except ValueError as exc:
                return Response(
                    display_text=f"I could not resolve that media folder: {exc}",
                    speech_text="I could not resolve that media folder.",
                    response_type=ResponseType.CLARIFICATION,
                    severity="warning",
                    speak=True,
                )
        return intent.model_copy(update={"arguments": arguments})

    @staticmethod
    def _looks_like_unresolved_file_reference(value: str) -> bool:
        candidate = value.strip()
        if not candidate or "/" in candidate or "\\" in candidate or ":" in candidate:
            return False
        first = candidate.split(maxsplit=1)[0].casefold()
        if first in KNOWN_USER_FOLDERS:
            return False
        if re.search(
            r"\b(?:in|inside|on|from)\s+(?:my\s+|the\s+)?"
            r"(?:desktop|documents?|downloads?|pictures?|music|videos?)\b",
            candidate,
            flags=re.IGNORECASE,
        ):
            return True
        return bool(
            re.match(r"^(?:the|that|this)\s+", candidate, flags=re.IGNORECASE) and " " in candidate
        )

    def _resolve_named_folder_path(
        self, value: str, *, session_id: str, request_text: str
    ) -> str | Response:
        folded = value.casefold()
        token = ASSISTANT_FOLDER_TOKEN.casefold()
        if folded != token and not (
            folded.startswith(token + "/") or folded.startswith(token + "\\")
        ):
            return value
        folder = self._find_allowed_folder("assistant", session_id=session_id)
        if folder is None:
            example_path = str(Path.home() / "Desktop")
            self._pending_clarification_follow_up = PendingClarificationFollowUp(
                original_text=request_text,
                clarification=("Which folder am I allowed to search for the Assistant folder?"),
                folder_name="assistant",
                session_id=session_id,
                kind="folder_search",
            )
            return Response(
                display_text=(
                    "I could not find an Assistant folder inside the folders I "
                    "am already allowed to search. Reply with the folder I may "
                    "search for this request, for example Desktop or "
                    f"{example_path}. I will use your next message as the "
                    "follow-up."
                ),
                speech_text=(
                    "I could not find an Assistant folder in the folders I can "
                    "search. Reply with a folder I may search for this request."
                ),
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )
        suffix = value[len(ASSISTANT_FOLDER_TOKEN) :].lstrip("/\\")
        return str((folder / suffix).resolve())

    def _find_allowed_folder(self, folder_name: str, *, session_id: str) -> Path | None:
        roots = self.engine.permissions.allowed_targets(
            user_id="local-user",
            capability="filesystem.read",
            session_id=session_id,
        )
        matches: list[Path] = []
        for root_text in roots:
            root = Path(root_text)
            if not root.exists():
                continue
            if root.is_file():
                root = root.parent
            matches.extend(self._matching_directories(root, folder_name))
        if not matches:
            return None
        return sorted(
            set(matches),
            key=lambda path: (len(path.parts), str(path)),
        )[0]

    @staticmethod
    def _matching_directories(root: Path, folder_name: str) -> list[Path]:
        needle = folder_name.casefold()
        matches: list[Path] = []
        try:
            if root.name.casefold() == needle:
                matches.append(root.resolve())
            for path in root.rglob("*"):
                if path.is_dir() and path.name.casefold() == needle:
                    matches.append(path.resolve())
        except (OSError, PermissionError):
            return matches
        return matches

    def _set_pending_clarification(
        self,
        *,
        original_text: str,
        clarification: str,
        session_id: str,
        kind: str = "generic",
        folder_name: str | None = None,
    ) -> None:
        self._pending_clarification_follow_up = PendingClarificationFollowUp(
            original_text=original_text,
            clarification=clarification,
            folder_name=folder_name,
            session_id=session_id,
            kind=kind,
        )

    async def _handle_pending_clarification_follow_up(
        self,
        correlation_id: UUID,
        text: str,
        pending: PendingClarificationFollowUp,
        *,
        session_id: str,
        invoked_by_voice: bool,
    ) -> tuple[Response, ExecutionOutcome]:
        reset_kind, replacement = self._pending_follow_up_reset(text)
        if reset_kind == "cancel":
            self._pending_clarification_follow_up = None
            response = Response(
                display_text="Cancelled the pending follow-up.",
                speech_text="Cancelled.",
                response_type=ResponseType.CONTROL,
                severity="warning",
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name="clarification.follow_up",
                outcome_label="cancelled",
                invoked_by_voice=invoked_by_voice,
            )
        if reset_kind == "new_request":
            self._pending_clarification_follow_up = None
            if replacement:
                return await self.handle_text(
                    replacement,
                    session_id=session_id,
                    invoked_by_voice=invoked_by_voice,
                )
            response = Response(
                display_text="Okay, I forgot the pending follow-up.",
                speech_text="Okay, I forgot the pending follow-up.",
                response_type=ResponseType.CONTROL,
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name="clarification.follow_up",
                outcome_label="cancelled",
                invoked_by_voice=invoked_by_voice,
            )

        standalone_route = self.router.route(text)
        if (
            not standalone_route.needs_llm
            and standalone_route.clarification is None
            and not standalone_route.intents
            and standalone_route.intent is not None
            and standalone_route.intent.name not in {"control.confirm", "control.deny"}
        ):
            self._pending_clarification_follow_up = None
            return await self.handle_text(
                text,
                session_id=session_id,
                invoked_by_voice=invoked_by_voice,
            )

        if pending.kind == "folder_search":
            return await self._handle_pending_folder_search_follow_up(
                correlation_id,
                text,
                pending,
                session_id=session_id,
                invoked_by_voice=invoked_by_voice,
            )

        if resolved := self._named_skill_follow_up_intent(pending, text):
            self._pending_clarification_follow_up = None
            return await self._handle_planner_intent(
                correlation_id,
                pending.original_text,
                resolved,
                session_id=session_id,
                invoked_by_voice=invoked_by_voice,
            )

        if resolved := self._file_search_limit_follow_up_intent(pending, text):
            self._pending_clarification_follow_up = None
            return await self._handle_planner_intent(
                correlation_id,
                pending.original_text,
                resolved,
                session_id=session_id,
                invoked_by_voice=invoked_by_voice,
            )

        follow_up_text = self._clarification_follow_up_request_text(pending, text)
        self._pending_clarification_follow_up = None
        return await self.handle_text(
            follow_up_text,
            session_id=session_id,
            invoked_by_voice=invoked_by_voice,
        )

    def _named_skill_follow_up_intent(
        self,
        pending: PendingClarificationFollowUp,
        answer: str,
    ) -> Intent | None:
        """Honor an explicit skill choice without asking the same question again."""
        route = self.router.route(pending.original_text)
        if route.needs_llm or route.clarification or route.intents or route.intent is None:
            return None
        try:
            manifest = self.engine.registry.get(route.intent.name).manifest
        except KeyError:
            return None
        labels = {
            manifest.display_name.casefold(),
            manifest.skill_id.casefold().replace(".", " ").replace("_", " "),
            manifest.skill_id.rsplit(".", 1)[-1].casefold().replace("_", " "),
        }
        folded = " ".join(answer.casefold().split())
        if not any(re.search(rf"\b{re.escape(label)}\b", folded) for label in labels):
            return None
        return route.intent

    @staticmethod
    def _pending_follow_up_reset(text: str) -> tuple[str | None, str | None]:
        value = text.strip()
        folded = value.casefold()
        if folded in {
            "cancel",
            "stop",
            "never mind",
            "never mind that",
            "nevermind",
            "nevermind that",
            "ignore that",
            "forget that",
            "forget it",
            "skip it",
            "no",
        }:
            return "cancel", None

        prefixes = (
            "new request",
            "start a new request",
            "new command",
            "start a new command",
            "different request",
            "separate request",
            "start over",
            "instead",
        )
        for prefix in prefixes:
            if folded == prefix:
                return "new_request", None
            if folded.startswith(prefix + ":"):
                return "new_request", value[len(prefix) + 1 :].strip() or None
            if folded.startswith(prefix + " "):
                return "new_request", value[len(prefix) :].strip() or None

        patterns = (
            r"(?:cancel|ignore|forget)\s+(?:that|it)\s+and\s+(?P<request>.+)",
            r"(?:actually|instead),?\s+(?P<request>.+)",
        )
        for pattern in patterns:
            match = re.fullmatch(pattern, value, flags=re.IGNORECASE)
            if match:
                return "new_request", match.group("request").strip()
        return None, None

    def _clarification_follow_up_request_text(
        self, pending: PendingClarificationFollowUp, answer: str
    ) -> str:
        timer_text = self._timer_follow_up_request_text(pending, answer)
        if timer_text:
            return timer_text
        conversion_text = self._conversion_follow_up_request_text(pending, answer)
        if conversion_text:
            return conversion_text
        calendar_text = self._calendar_follow_up_request_text(pending, answer)
        if calendar_text:
            return calendar_text
        return (
            f"{pending.original_text}\n"
            f"Clarifying question: {pending.clarification}\n"
            f"Follow-up answer: {answer}\n"
            "Use the follow-up answer to complete the previous request."
        )

    @staticmethod
    def _calendar_follow_up_request_text(
        pending: PendingClarificationFollowUp, answer: str
    ) -> str | None:
        if "calendar" not in pending.original_text.casefold():
            return None
        if "what event should" not in pending.clarification.casefold():
            return None
        value = answer.strip()
        return f"add {value} to my calendar" if value else None

    @staticmethod
    def _file_search_limit_follow_up_intent(
        pending: PendingClarificationFollowUp, answer: str
    ) -> Intent | None:
        if "limit" not in pending.clarification.casefold():
            return None
        value = answer.strip()
        if not re.fullmatch(r"\d+", value):
            return None
        limit = int(value)
        if not 1 <= limit <= 100:
            return None
        route = DeterministicRouter().route(pending.original_text)
        if route.intent is None or route.intent.name != "windows.file_search":
            return None
        arguments = dict(route.intent.arguments)
        arguments["limit"] = limit
        return route.intent.model_copy(update={"arguments": arguments})

    @staticmethod
    def _timer_follow_up_request_text(
        pending: PendingClarificationFollowUp, answer: str
    ) -> str | None:
        if "timer" not in pending.original_text.casefold():
            return None
        if "how long" not in pending.clarification.casefold():
            return None
        value = answer.strip()
        if not re.search(r"\b(seconds?|minutes?|hours?)\b", value, re.IGNORECASE):
            match = re.search(
                r"\b(seconds?|minutes?|hours?)\b",
                pending.original_text,
                flags=re.IGNORECASE,
            )
            if match:
                value = f"{value} {match.group(1)}"
        return f"set timer for {value}"

    @staticmethod
    def _conversion_follow_up_request_text(
        pending: PendingClarificationFollowUp, answer: str
    ) -> str | None:
        if "what value" not in pending.clarification.casefold():
            return None
        match = re.fullmatch(
            r"convert\s+.+?\s+"
            r"(?P<from>kilometers?|miles?|meters?|feet|kilograms?|pounds?|"
            r"liters?|gallons?|celsius|fahrenheit)\s+to\s+"
            r"(?P<to>kilometers?|miles?|meters?|feet|kilograms?|pounds?|"
            r"liters?|gallons?|celsius|fahrenheit)",
            pending.original_text.strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return f"convert {answer.strip()} {match.group('from')} to {match.group('to')}"

    async def _handle_pending_folder_search_follow_up(
        self,
        correlation_id: UUID,
        text: str,
        pending: PendingClarificationFollowUp,
        *,
        session_id: str,
        invoked_by_voice: bool,
    ) -> tuple[Response, ExecutionOutcome]:
        folded = text.strip().casefold()
        if folded in {"cancel", "stop", "never mind", "never mind that"}:
            self._pending_clarification_follow_up = None
            response = Response(
                display_text="Cancelled the pending follow-up.",
                speech_text="Cancelled.",
                response_type=ResponseType.CONTROL,
                severity="warning",
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name="clarification.follow_up",
                outcome_label="cancelled",
                invoked_by_voice=invoked_by_voice,
            )

        root = self._folder_search_root_from_follow_up(text)
        if root is None:
            example_path = str(Path.home() / "Desktop")
            response = Response(
                display_text=(
                    "I am waiting for the folder to search for the previous "
                    "request. Reply with a folder name or path, such as Desktop "
                    f"or {example_path}, or say cancel."
                ),
                speech_text=("Please reply with a folder name or path, or say cancel."),
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name="clarification.follow_up",
                outcome_label="needs_clarification",
                invoked_by_voice=invoked_by_voice,
            )

        if root.is_file():
            root = root.parent
        if not root.exists() or not root.is_dir():
            response = Response(
                display_text=(
                    f"I could not find that folder: {root}. Reply with an "
                    "existing folder I may search, or say cancel."
                ),
                speech_text=(
                    "I could not find that folder. Reply with an existing folder, or say cancel."
                ),
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name="clarification.follow_up",
                outcome_label="needs_clarification",
                invoked_by_voice=invoked_by_voice,
            )

        grant_session_id = pending.session_id if pending.session_id == session_id else session_id
        self.engine.permissions.grant(
            user_id="local-user",
            skill_id="windows.file_search",
            capability="filesystem.read",
            grant_type=GrantType.SESSION,
            targets=(str(root.resolve()),),
            session_id=grant_session_id,
            correlation_id=str(correlation_id),
        )
        self._pending_clarification_follow_up = None
        return await self.handle_text(
            pending.original_text,
            session_id=grant_session_id,
            invoked_by_voice=invoked_by_voice,
        )

    @staticmethod
    def _folder_search_root_from_follow_up(text: str) -> Path | None:
        candidate = text.strip().strip("\"'").rstrip(".").strip()
        if not candidate:
            return None
        folded = candidate.casefold()
        if folded in {
            "yes",
            "yeah",
            "ok",
            "okay",
            "there",
            "that folder",
            "the folder",
        }:
            return None

        patterns = (
            r"(?:you\s+(?:can|may)\s+)?(?:search|look in|check|use|try)\s+(?P<path>.+)",
            r"(?:it'?s|it is|the folder is|folder is)\s+(?P<path>.+)",
            r"(?:in|on)\s+(?P<path>.+)",
        )
        for pattern in patterns:
            match = re.fullmatch(pattern, candidate, flags=re.IGNORECASE)
            if match:
                candidate = match.group("path").strip()
                break

        candidate = re.sub(r"^(?:the|my)\s+", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s+folder$", "", candidate, flags=re.IGNORECASE)
        if candidate.casefold() in {
            "desktop",
            "documents",
            "downloads",
            "pictures",
            "music",
            "videos",
        }:
            return resolve_user_path(candidate)

        try:
            return resolve_user_path(candidate)
        except ValueError:
            return None

    async def handle_text(
        self,
        text: str,
        *,
        session_id: str = "desktop",
        invoked_by_voice: bool = False,
        correlation_id: UUID | None = None,
    ) -> tuple[Response, ExecutionOutcome]:
        correlation_id = correlation_id or uuid4()
        request_started = self._voice_interaction_starts.pop(
            correlation_id,
            time.perf_counter(),
        )
        self._metric_request_starts[correlation_id] = request_started
        request_outcome = "error"
        with self._request_state_lock:
            self._active_request_correlations.append(correlation_id)
        provider = getattr(self.planner, "provider", None)
        metric_scope = getattr(provider, "metric_scope", None)
        scope = metric_scope(str(correlation_id)) if callable(metric_scope) else nullcontext()
        try:
            with scope:
                result = await self._handle_text(
                    correlation_id,
                    text,
                    session_id=session_id,
                    invoked_by_voice=invoked_by_voice,
                )
            request_outcome = result[0].response_type.value
            return result
        except asyncio.CancelledError:
            request_outcome = "cancelled"
            raise
        finally:
            if self.metrics is not None:
                self.metrics.record(
                    correlation_id=str(correlation_id),
                    stage=MetricStage.REQUEST_TOTAL,
                    duration_ms=(time.perf_counter() - request_started) * 1000,
                    outcome=request_outcome,
                    dimensions=MetricDimensions(
                        interaction=(
                            InteractionMode.VOICE if invoked_by_voice else InteractionMode.TYPED
                        )
                    ),
                )
                working_set = process_working_set_bytes()
                if working_set:
                    self.metrics.record(
                        correlation_id=str(correlation_id),
                        stage=MetricStage.RESOURCE_MEMORY,
                        value=float(working_set),
                        outcome="sampled",
                        dimensions=MetricDimensions(
                            interaction=(
                                InteractionMode.VOICE if invoked_by_voice else InteractionMode.TYPED
                            )
                        ),
                    )
                self.metrics.record(
                    correlation_id=str(correlation_id),
                    stage=MetricStage.RESOURCE_QUEUE_DEPTH,
                    value=float(len(self._active_request_correlations)),
                    outcome="sampled",
                    dimensions=MetricDimensions(
                        interaction=(
                            InteractionMode.VOICE if invoked_by_voice else InteractionMode.TYPED
                        )
                    ),
                )
            with self._request_state_lock:
                if correlation_id in self._active_request_correlations:
                    self._active_request_correlations.remove(correlation_id)
            self._metric_request_starts.pop(correlation_id, None)
            self._response_visible_recorded.discard(correlation_id)

    async def _handle_text(
        self,
        correlation_id: UUID,
        text: str,
        *,
        session_id: str = "desktop",
        invoked_by_voice: bool = False,
    ) -> tuple[Response, ExecutionOutcome]:
        self._active_correlation = correlation_id
        await self.events.publish(
            PipelineEvent(
                correlation_id=correlation_id,
                state=PipelineState.THINKING,
                message="Interpreting command",
                trace=ActivityTrace(
                    phase=TracePhase.UNDERSTANDING,
                    label="Understanding",
                    detail="Interpreting the request and selecting a bounded action.",
                ),
            )
        )
        if self._pending_clarification_follow_up is not None:
            folded = text.strip().casefold()
            if not folded.startswith("forget"):
                return await self._handle_pending_clarification_follow_up(
                    correlation_id,
                    text,
                    self._pending_clarification_follow_up,
                    session_id=session_id,
                    invoked_by_voice=invoked_by_voice,
                )
            self._pending_clarification_follow_up = None
        self._select_contextual_result_reference(text)
        route_started = time.perf_counter()
        route = self.router.route(text)
        route_duration = (time.perf_counter() - route_started) * 1000
        intent = route.intent
        await self._record_routing(
            correlation_id,
            intent,
            route_duration,
            interaction=(InteractionMode.VOICE if invoked_by_voice else InteractionMode.TYPED),
            path=(RequestPath.PLANNER if route.needs_llm else RequestPath.DETERMINISTIC),
        )
        if route.clarification:
            self._set_pending_clarification(
                original_text=text,
                clarification=route.clarification,
                session_id=session_id,
            )
            response = Response(
                display_text=route.clarification,
                speech_text=route.clarification,
                response_type=ResponseType.CLARIFICATION,
            )
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=intent,
                outcome_label="needs_clarification",
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name="clarification",
                outcome_label="needs_clarification",
                invoked_by_voice=invoked_by_voice,
            )
        if route.needs_llm:
            planner_started = time.perf_counter()
            planner_resolution = await self.planner_flow.resolve(
                self.planner,
                text,
                intent,
                tuple(self._session_context),
                interpret_goal=lambda: self._interpret_planner_goal(
                    correlation_id,
                    text,
                    session_id=session_id,
                ),
            )
            if self.metrics is not None:
                planner_outcome = (
                    "unavailable"
                    if planner_resolution.unavailable
                    else "failed"
                    if planner_resolution.error is not None
                    else "completed"
                )
                self.metrics.record(
                    correlation_id=str(correlation_id),
                    stage=MetricStage.PLANNER_TOTAL,
                    duration_ms=(time.perf_counter() - planner_started) * 1000,
                    outcome=planner_outcome,
                    dimensions=MetricDimensions(
                        interaction=(
                            InteractionMode.VOICE if invoked_by_voice else InteractionMode.TYPED
                        ),
                        path=RequestPath.PLANNER,
                    ),
                )
            if planner_resolution.unavailable:
                response = self._planner_unavailable_response()
                response = self._with_reasoning_steps(
                    response,
                    request_text=text,
                    intent=None,
                    outcome_label="planner_unavailable",
                )
                return await self._finish_interaction(
                    correlation_id,
                    text,
                    response,
                    intent_name="planner.unavailable",
                    outcome_label="failed",
                    invoked_by_voice=invoked_by_voice,
                )
            if planner_resolution.error is not None:
                exc = planner_resolution.error
                logger.warning("Local planner failed", exc_info=exc)
                response = self._planner_failed_response(exc)
                response = self._with_reasoning_steps(
                    response,
                    request_text=text,
                    intent=None,
                    outcome_label="planner_failed",
                )
                return await self._finish_interaction(
                    correlation_id,
                    text,
                    response,
                    intent_name="planner.failed",
                    outcome_label="failed",
                    invoked_by_voice=invoked_by_voice,
                )
            if planner_resolution.decision is not None:
                return await self._handle_planner_decision(
                    correlation_id,
                    text,
                    planner_resolution.decision,
                    session_id=session_id,
                    invoked_by_voice=invoked_by_voice,
                )
            intent = planner_resolution.intent
        assert intent is not None
        if route.intents:
            return await self._handle_intent_plan(
                correlation_id,
                text,
                route.intents,
                session_id=session_id,
                invoked_by_voice=invoked_by_voice,
            )
        if intent.name in {"conversation.answer", "conversation.reject"}:
            response = Response(
                display_text=intent.arguments["text"],
                speech_text=intent.arguments["text"],
                response_type=ResponseType.ANSWER,
                severity="warning" if intent.name == "conversation.reject" else "info",
            )
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=intent,
                outcome_label="answered",
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name=intent.name,
                outcome_label="answered",
                invoked_by_voice=invoked_by_voice,
            )
        if intent.name == "control.cancel":
            await self.cancel(exclude_correlation=correlation_id)
            response = Response(
                display_text="Cancelled.",
                speech_text="Cancelled.",
                response_type=ResponseType.CONTROL,
            )
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=intent,
                outcome_label="cancelled",
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name=intent.name,
                outcome_label="cancelled",
                invoked_by_voice=invoked_by_voice,
            )
        if intent.name == "control.confirm":
            pending = self._pending_confirmation
            if pending is None or pending.confirmation is None or pending.call is None:
                response = Response(
                    display_text="There is no pending action to confirm.",
                    speech_text="There is no pending action to confirm.",
                    response_type=ResponseType.CONTROL,
                    severity="warning",
                )
                return await self._finish_interaction(
                    correlation_id,
                    text,
                    response,
                    intent_name=intent.name,
                    outcome_label="nothing_to_confirm",
                    invoked_by_voice=invoked_by_voice,
                )
            request = pending.confirmation
            if request.requires_typed_token:
                response = Response(
                    display_text=(
                        "This critical action requires the displayed confirmation token; "
                        "use the on-screen confirmation prompt."
                    ),
                    speech_text=("This critical action requires the displayed confirmation token."),
                    response_type=ResponseType.CONFIRMATION,
                    severity="warning",
                )
                return await self._finish_interaction(
                    correlation_id,
                    text,
                    response,
                    pending,
                    intent_name=intent.name,
                    outcome_label="typed_token_required",
                    invoked_by_voice=invoked_by_voice,
                )
            self._pending_confirmation = None
            confirmed = ExecutionOutcome(
                result=await self.engine.execute(
                    pending.call,
                    confirmation_id=request.confirmation_id,
                ),
                call=pending.call,
            )
            response = self._response_from_outcome(confirmed).model_copy(
                update={"response_type": ResponseType.CONTROL}
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                confirmed,
                intent_name=intent.name,
                outcome_label=self._outcome_label(confirmed),
                invoked_by_voice=invoked_by_voice,
            )
        if intent.name == "control.deny":
            pending = self._pending_confirmation
            denied = bool(pending and pending.confirmation)
            if denied:
                assert pending is not None and pending.confirmation is not None
                self.engine.confirmations.deny(pending.confirmation.confirmation_id)
                self._pending_confirmation = None
            message = "Pending action denied." if denied else "There is no pending action to deny."
            response = Response(
                display_text=message,
                speech_text=message,
                response_type=ResponseType.CONTROL,
                severity="warning",
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name=intent.name,
                outcome_label="denied" if denied else "nothing_to_deny",
                invoked_by_voice=invoked_by_voice,
            )
        if intent.name == "control.undo":
            outcome = ExecutionOutcome(result=await self.engine.undo_last(correlation_id))
            response = self._response_from_outcome(outcome).model_copy(
                update={"response_type": ResponseType.CONTROL}
            )
            outcome_label = self._outcome_label(outcome)
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=intent,
                outcome_label=outcome_label,
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                outcome,
                intent_name=intent.name,
                outcome_label=outcome_label,
                invoked_by_voice=invoked_by_voice,
            )
        if intent.name == "control.forget":
            self.state.clear()
            self.media_sessions.clear()
            response = Response(
                display_text="I forgot the current session context.",
                speech_text="Session context cleared.",
                response_type=ResponseType.CONTROL,
            )
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=intent,
                outcome_label="forgotten",
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name=intent.name,
                outcome_label="forgotten",
                invoked_by_voice=invoked_by_voice,
                add_session_context=False,
            )
        if intent.name == "control.correct":
            if self._session_context:
                self._session_context.pop()
            return await self.handle_text(
                intent.arguments["text"],
                session_id=session_id,
                invoked_by_voice=invoked_by_voice,
            )
        if intent.name in {"control.result_select", "control.file_select"}:
            response = self._select_result_item(intent.arguments["selector"])
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=intent,
                outcome_label="selected",
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name=intent.name,
                outcome_label="selected",
                invoked_by_voice=invoked_by_voice,
            )
        file_resolution = self.file_resolver.resolve(
            intent,
            session_id=session_id,
            request_text=text,
        )
        if file_resolution.clarification is not None:
            response = self._with_reasoning_steps(
                file_resolution.clarification,
                request_text=text,
                intent=intent,
                outcome_label="needs_clarification",
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name=intent.name,
                outcome_label="needs_clarification",
                invoked_by_voice=invoked_by_voice,
            )
        assert file_resolution.intent is not None
        intent = file_resolution.intent
        call = SkillCall(
            skill_id=intent.name,
            arguments=intent.arguments,
            permission_context=PermissionContext(session_id=session_id),
            correlation_id=correlation_id,
        )
        try:
            outcome = await self.engine.prepare(call)
        except Exception as exc:
            logger.warning(
                "Command preparation failed",
                exc_info=exc,
                extra={
                    "correlation_id": str(correlation_id),
                    "stage": "preparation",
                    "skill_id": intent.name,
                    "outcome": "failed",
                },
            )
            response = Response(
                display_text=f"I could not prepare that command: {exc}",
                speech_text="I could not prepare that command.",
                response_type=ResponseType.ERROR,
                severity="warning",
            )
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=intent,
                outcome_label="preparation_failed",
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name=intent.name,
                outcome_label="preparation_failed",
                invoked_by_voice=invoked_by_voice,
            )
        response = self._response_from_outcome(outcome)
        outcome_label = self._outcome_label(outcome)
        response = self._with_reasoning_steps(
            response,
            request_text=text,
            intent=intent,
            outcome_label=outcome_label,
            step_summaries=(
                f"Prepare {intent.name} with {json.dumps(intent.arguments, default=str)}",
            ),
        )
        return await self._finish_interaction(
            correlation_id,
            text,
            response,
            outcome,
            intent_name=intent.name,
            outcome_label=outcome_label,
            invoked_by_voice=invoked_by_voice,
        )

    async def _handle_planner_decision(
        self,
        correlation_id: UUID,
        text: str,
        decision: PlannerDecision,
        *,
        session_id: str,
        invoked_by_voice: bool,
    ) -> tuple[Response, ExecutionOutcome]:
        if decision.clarification:
            self._set_pending_clarification(
                original_text=text,
                clarification=decision.clarification,
                session_id=session_id,
            )
            response = Response(
                display_text=decision.clarification,
                speech_text=decision.clarification,
                response_type=ResponseType.CLARIFICATION,
            )
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=None,
                outcome_label="needs_clarification",
                interpretation=decision.interpretation,
                context=decision.context,
                retrieved=decision.retrieved,
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name="planner.clarification",
                outcome_label="needs_clarification",
                invoked_by_voice=invoked_by_voice,
            )
        if decision.steps:
            return await self._handle_planned_steps(
                correlation_id,
                text,
                decision.steps,
                session_id=session_id,
                invoked_by_voice=invoked_by_voice,
            )
        if decision.intent is None:
            response = Response(
                display_text="I could not turn that into an available local action.",
                speech_text="I could not turn that into an available action.",
                response_type=ResponseType.ERROR,
                severity="warning",
            )
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=None,
                outcome_label="planner_empty",
                interpretation=decision.interpretation,
                context=decision.context,
                retrieved=decision.retrieved,
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name="planner.empty",
                outcome_label="failed",
                invoked_by_voice=invoked_by_voice,
            )
        if (
            decision.allow_replanning
            and decision.interpretation is not None
            and decision.context is not None
            and decision.intent.name not in {"conversation.answer", "conversation.reject"}
            and hasattr(self.planner, "revise_plan")
        ):
            return await self._handle_replanning_intent(
                correlation_id,
                text,
                decision.intent,
                interpretation=decision.interpretation,
                context=decision.context,
                retrieved=decision.retrieved,
                session_id=session_id,
                invoked_by_voice=invoked_by_voice,
            )
        return await self._handle_planner_intent(
            correlation_id,
            text,
            decision.intent,
            session_id=session_id,
            invoked_by_voice=invoked_by_voice,
            interpretation=decision.interpretation,
            context=decision.context,
            retrieved=decision.retrieved,
        )

    def has_replanning_continuation(self, outcome: ExecutionOutcome) -> bool:
        call = outcome.call
        return bool(call and call.correlation_id in self._replanning_continuations)

    def discard_replanning_continuation(self, outcome: ExecutionOutcome) -> None:
        if outcome.call is not None:
            self._replanning_continuations.pop(outcome.call.correlation_id, None)
        if self._pending_confirmation is outcome:
            self._pending_confirmation = None

    async def continue_replanning(
        self,
        outcome: ExecutionOutcome,
    ) -> tuple[Response, ExecutionOutcome]:
        if outcome.call is None:
            raise ValueError("A replanning continuation requires its pending skill call")
        state = self._replanning_continuations.pop(outcome.call.correlation_id, None)
        if state is None:
            raise ValueError("No replanning continuation is pending for this call")
        prepared = await self.engine.prepare(outcome.call)
        return await self._resume_replanning(state, prepared)

    async def confirm_replanning(
        self,
        outcome: ExecutionOutcome,
        *,
        typed_token: str | None = None,
    ) -> tuple[Response, ExecutionOutcome]:
        if outcome.call is None or outcome.confirmation is None:
            raise ValueError("A confirmed replanning continuation requires its pending call")
        state = self._replanning_continuations.pop(outcome.call.correlation_id, None)
        if state is None:
            raise ValueError("No replanning continuation is pending for this call")
        if self._pending_confirmation is outcome:
            self._pending_confirmation = None
        result = await self.engine.execute(
            outcome.call,
            confirmation_id=outcome.confirmation.confirmation_id,
            typed_token=typed_token,
        )
        return await self._resume_replanning(
            state,
            ExecutionOutcome(result=result, call=outcome.call),
        )

    async def _resume_replanning(
        self,
        state: ReplanningContinuation,
        prepared_outcome: ExecutionOutcome,
    ) -> tuple[Response, ExecutionOutcome]:
        return await self._handle_replanning_intent(
            state.correlation_id,
            state.text,
            state.current_intent,
            interpretation=state.interpretation,
            context=state.context,
            retrieved=state.retrieved,
            session_id=state.session_id,
            invoked_by_voice=state.invoked_by_voice,
            max_iterations=state.max_iterations,
            prior_observations=state.observations,
            prior_step_summaries=state.step_summaries,
            start_iteration=state.iteration,
            prepared_outcome=prepared_outcome,
        )

    async def _handle_replanning_intent(
        self,
        correlation_id: UUID,
        text: str,
        intent: Intent,
        *,
        interpretation,
        context: ContextSnapshot,
        retrieved: tuple[RetrievedContextItem, ...],
        session_id: str,
        invoked_by_voice: bool,
        max_iterations: int = 5,
        prior_observations: tuple[dict[str, Any], ...] = (),
        prior_step_summaries: tuple[str, ...] = (),
        start_iteration: int = 1,
        prepared_outcome: ExecutionOutcome | None = None,
    ) -> tuple[Response, ExecutionOutcome]:
        assert self.planner is not None
        observations = list(prior_observations)
        current_intent = intent
        last_outcome = prepared_outcome or ExecutionOutcome()
        step_summaries = list(prior_step_summaries)
        for iteration in range(start_iteration, max_iterations + 1):
            call = SkillCall(
                skill_id=current_intent.name,
                caller="planner",
                arguments=current_intent.arguments,
                permission_context=PermissionContext(session_id=session_id),
                correlation_id=correlation_id,
            )
            if prepared_outcome is not None:
                outcome = prepared_outcome
                prepared_outcome = None
            else:
                try:
                    outcome = await self.engine.prepare(call)
                except Exception as exc:
                    logger.warning("Replanned command preparation failed", exc_info=exc)
                    self._replanning_continuations.pop(correlation_id, None)
                    response = Response(
                        display_text=f"I could not prepare that command: {exc}",
                        speech_text="I could not prepare that command.",
                        response_type=ResponseType.ERROR,
                        severity="warning",
                    )
                    response = self._with_reasoning_steps(
                        response,
                        request_text=text,
                        intent=current_intent,
                        outcome_label="preparation_failed",
                        step_summaries=tuple(step_summaries),
                        interpretation=interpretation,
                        context=context,
                        retrieved=retrieved,
                    )
                    return await self._finish_interaction(
                        correlation_id,
                        text,
                        response,
                        intent_name=current_intent.name,
                        outcome_label="preparation_failed",
                        invoked_by_voice=invoked_by_voice,
                    )
            last_outcome = outcome
            attempt_summary = (
                f"Prepare {current_intent.name} with "
                f"{json.dumps(current_intent.arguments, default=str)}"
            )
            outcome_label = self._outcome_label(outcome)
            if (
                outcome.confirmation
                or outcome.permission_requests
                or outcome.registration_requests
                or outcome.result is None
                or outcome.result.status.value != "completed"
            ):
                if (
                    outcome.confirmation
                    or outcome.permission_requests
                    or outcome.registration_requests
                ):
                    self._replanning_continuations[correlation_id] = ReplanningContinuation(
                        correlation_id=correlation_id,
                        text=text,
                        current_intent=current_intent,
                        interpretation=interpretation,
                        context=context,
                        retrieved=retrieved,
                        session_id=session_id,
                        invoked_by_voice=invoked_by_voice,
                        observations=tuple(observations),
                        step_summaries=tuple(step_summaries),
                        iteration=iteration,
                        max_iterations=max_iterations,
                    )
                else:
                    self._replanning_continuations.pop(correlation_id, None)
                response = self._response_from_outcome(outcome)
                response = self._with_reasoning_steps(
                    response,
                    request_text=text,
                    intent=current_intent,
                    outcome_label=outcome_label,
                    step_summaries=(*step_summaries, attempt_summary),
                    interpretation=interpretation,
                    context=context,
                    retrieved=retrieved,
                )
                return await self._finish_interaction(
                    correlation_id,
                    text,
                    response,
                    outcome,
                    intent_name=current_intent.name,
                    outcome_label=outcome_label,
                    invoked_by_voice=invoked_by_voice,
                )
            step_summaries.append(attempt_summary)
            observations.append(
                self._observation_from_outcome(
                    current_intent,
                    outcome,
                    iteration=iteration,
                    outcome_label=outcome_label,
                )
            )
            revision = await self.planner.revise_plan(
                text,
                interpretation=interpretation,
                context=context,
                retrieved=retrieved,
                observations=tuple(observations),
            )
            if revision.disposition == "next_step":
                if revision.skill_id.strip():
                    current_intent = Intent(
                        name=revision.skill_id,
                        arguments=revision.arguments,
                        confidence=revision.confidence,
                        source="llm",
                    )
                    continue
                revision = revision.model_copy(
                    update={
                        "disposition": "clarify",
                        "arguments": {},
                        "clarification": (
                            "I could not map the next step to a registered skill. "
                            "What exact action should I take next?"
                        ),
                    }
                )
            if revision.disposition == "clarify":
                question = revision.clarification or "What should I do next?"
                self._set_pending_clarification(
                    original_text=text,
                    clarification=question,
                    session_id=session_id,
                )
                response = Response(
                    display_text=question,
                    speech_text=question,
                    response_type=ResponseType.CLARIFICATION,
                )
                response = self._with_reasoning_steps(
                    response,
                    request_text=text,
                    intent=current_intent,
                    outcome_label="needs_clarification",
                    step_summaries=tuple(step_summaries),
                    interpretation=interpretation,
                    context=context,
                    retrieved=retrieved,
                )
                return await self._finish_interaction(
                    correlation_id,
                    text,
                    response,
                    last_outcome,
                    intent_name="planner.replan",
                    outcome_label="needs_clarification",
                    invoked_by_voice=invoked_by_voice,
                )
            self._replanning_continuations.pop(correlation_id, None)
            message = revision.response or last_outcome.result.summary
            if revision.disposition == "stop":
                response = self._response_from_outcome(last_outcome).model_copy(
                    update={
                        "display_text": message,
                        "speech_text": message,
                        "response_type": ResponseType.STATUS,
                        "severity": "success",
                    }
                )
            else:
                response = Response(
                    display_text=message,
                    speech_text=message,
                    response_type=ResponseType.ERROR,
                    severity="warning",
                )
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=current_intent,
                outcome_label=f"replan_{revision.disposition}",
                step_summaries=tuple(step_summaries),
                interpretation=interpretation,
                context=context,
                retrieved=retrieved,
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                last_outcome,
                intent_name="planner.replan",
                outcome_label=f"replan_{revision.disposition}",
                invoked_by_voice=invoked_by_voice,
            )
        self._replanning_continuations.pop(correlation_id, None)
        response = Response(
            display_text=(
                "I stopped after reaching the replanning limit before taking more steps."
            ),
            speech_text="I stopped after reaching the replanning limit.",
            response_type=ResponseType.ERROR,
            severity="warning",
        )
        response = self._with_reasoning_steps(
            response,
            request_text=text,
            intent=current_intent,
            outcome_label="replan_limit",
            step_summaries=tuple(step_summaries),
            interpretation=interpretation,
            context=context,
            retrieved=retrieved,
        )
        return await self._finish_interaction(
            correlation_id,
            text,
            response,
            last_outcome,
            intent_name="planner.replan",
            outcome_label="replan_limit",
            invoked_by_voice=invoked_by_voice,
        )

    @staticmethod
    def _observation_from_outcome(
        intent: Intent,
        outcome: ExecutionOutcome,
        *,
        iteration: int,
        outcome_label: str,
    ) -> dict[str, Any]:
        result = outcome.result
        return {
            "iteration": iteration,
            "skill_id": intent.name,
            "arguments": intent.arguments,
            "outcome": outcome_label,
            "result_status": result.status.value if result else None,
            "summary": (result.summary[:500] if result else ""),
            "data_keys": (
                sorted(result.data.keys()) if result and isinstance(result.data, dict) else []
            ),
            "result_data": result.data if result else {},
        }

    async def _handle_planner_intent(
        self,
        correlation_id: UUID,
        text: str,
        intent: Intent,
        *,
        session_id: str,
        invoked_by_voice: bool,
        interpretation=None,
        context: ContextSnapshot | None = None,
        retrieved: tuple[Any, ...] = (),
    ) -> tuple[Response, ExecutionOutcome]:
        if intent.name in {"conversation.answer", "conversation.reject"}:
            response = Response(
                display_text=intent.arguments["text"],
                speech_text=intent.arguments["text"],
                response_type=ResponseType.ANSWER,
                severity="warning" if intent.name == "conversation.reject" else "info",
            )
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=intent,
                outcome_label="answered",
                interpretation=interpretation,
                context=context,
                retrieved=retrieved,
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name=intent.name,
                outcome_label="answered",
                invoked_by_voice=invoked_by_voice,
            )
        call = SkillCall(
            skill_id=intent.name,
            caller="planner",
            arguments=intent.arguments,
            permission_context=PermissionContext(session_id=session_id),
            correlation_id=correlation_id,
        )
        try:
            outcome = await self.engine.prepare(call)
        except Exception as exc:
            logger.warning(
                "Command preparation failed",
                exc_info=exc,
                extra={
                    "correlation_id": str(correlation_id),
                    "stage": "preparation",
                    "skill_id": intent.name,
                    "outcome": "failed",
                },
            )
            response = Response(
                display_text=f"I could not prepare that command: {exc}",
                speech_text="I could not prepare that command.",
                response_type=ResponseType.ERROR,
                severity="warning",
            )
            response = self._with_reasoning_steps(
                response,
                request_text=text,
                intent=intent,
                outcome_label="preparation_failed",
                interpretation=interpretation,
                context=context,
                retrieved=retrieved,
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name=intent.name,
                outcome_label="preparation_failed",
                invoked_by_voice=invoked_by_voice,
            )
        response = self._response_from_outcome(outcome)
        outcome_label = self._outcome_label(outcome)
        response = self._with_reasoning_steps(
            response,
            request_text=text,
            intent=intent,
            outcome_label=outcome_label,
            step_summaries=(
                f"Prepare {intent.name} with {json.dumps(intent.arguments, default=str)}",
            ),
            interpretation=interpretation,
            context=context,
            retrieved=retrieved,
        )
        return await self._finish_interaction(
            correlation_id,
            text,
            response,
            outcome,
            intent_name=intent.name,
            outcome_label=outcome_label,
            invoked_by_voice=invoked_by_voice,
        )

    async def _handle_intent_plan(
        self,
        correlation_id: UUID,
        text: str,
        intents: tuple[Intent, ...],
        *,
        session_id: str,
        invoked_by_voice: bool,
    ) -> tuple[Response, ExecutionOutcome]:
        steps = []
        previous_step_id: str | None = None
        for index, intent in enumerate(intents, 1):
            step_id = f"step_{index}"
            call = SkillCall(
                skill_id=intent.name,
                arguments=intent.arguments,
                permission_context=PermissionContext(session_id=session_id),
                correlation_id=correlation_id,
            )
            steps.append(
                PlanStep(
                    step_id=step_id,
                    call=call,
                    depends_on=(previous_step_id,) if previous_step_id else (),
                )
            )
            previous_step_id = step_id
        outcome = await self.engine.prepare_plan(ExecutionPlan(steps=tuple(steps)))
        response = self._response_from_outcome(outcome)
        step_summaries = tuple(
            f"Prepare {intent.name} with {json.dumps(intent.arguments, default=str)}"
            for intent in intents
        )
        response = self._with_reasoning_steps(
            response,
            request_text=text,
            intent=Intent(
                name="compound.plan",
                arguments={"steps": [intent.name for intent in intents]},
                confidence=min(intent.confidence for intent in intents),
                source="deterministic",
            ),
            outcome_label=self._outcome_label(outcome),
            step_summaries=step_summaries,
        )
        return await self._finish_interaction(
            correlation_id,
            text,
            response,
            outcome,
            intent_name="compound.plan",
            outcome_label=self._outcome_label(outcome),
            invoked_by_voice=invoked_by_voice,
        )

    async def _handle_planned_steps(
        self,
        correlation_id: UUID,
        text: str,
        steps: tuple[PlannedIntentStep, ...],
        *,
        session_id: str,
        invoked_by_voice: bool,
    ) -> tuple[Response, ExecutionOutcome]:
        plan_steps = []
        for step in steps:
            call = SkillCall(
                skill_id=step.intent.name,
                caller="planner",
                arguments=step.intent.arguments,
                permission_context=PermissionContext(session_id=session_id),
                correlation_id=correlation_id,
            )
            plan_steps.append(
                PlanStep(
                    step_id=step.step_id,
                    call=call,
                    depends_on=step.depends_on,
                    timeout_seconds=step.timeout_seconds,
                )
            )
        outcome = await self.engine.prepare_plan(ExecutionPlan(steps=tuple(plan_steps)))
        response = self._response_from_outcome(outcome)
        step_summaries = tuple(
            f"Prepare {step.step_id}: {step.intent.name} with "
            f"{json.dumps(step.intent.arguments, default=str)}"
            for step in steps
        )
        response = self._with_reasoning_steps(
            response,
            request_text=text,
            intent=Intent(
                name="planner.plan",
                arguments={"steps": [step.intent.name for step in steps]},
                confidence=min(step.intent.confidence for step in steps),
                source="llm",
            ),
            outcome_label=self._outcome_label(outcome),
            step_summaries=step_summaries,
        )
        return await self._finish_interaction(
            correlation_id,
            text,
            response,
            outcome,
            intent_name="planner.plan",
            outcome_label=self._outcome_label(outcome),
            invoked_by_voice=invoked_by_voice,
        )

    async def handle_transcript(self, transcript: Transcript) -> tuple[Response, ExecutionOutcome]:
        if not transcript.normalized_text.strip():
            response = Response(
                display_text="No speech was detected.",
                speech_text=None,
                response_type=ResponseType.STATUS,
                severity="warning",
                updates_display=False,
            )
            return await self._finish_voice_without_text_request(
                transcript.session_id,
                response,
                outcome="no_speech",
            )
        route = self.router.route(transcript.normalized_text)
        intent = route.intent
        voice_gate = evaluate_voice_gate(transcript, intent)
        if voice_gate.should_clarify:
            self._print_voice_gate_debug(
                transcript,
                intent,
                uncertain=voice_gate.uncertain,
                sensitive=voice_gate.sensitive,
                low_words=voice_gate.low_confidence_words,
                sensitive_argument_keys=voice_gate.sensitive_argument_keys,
            )
            response = Response(
                display_text=(
                    "I am not confident I heard the sensitive target correctly. "
                    "Please repeat the exact path, process, or device."
                ),
                speech_text=("Please repeat the exact target. I did not perform the action."),
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )
            return await self._finish_voice_without_text_request(
                transcript.session_id,
                response,
                outcome="needs_clarification",
            )
        return await self.handle_text(
            transcript.normalized_text,
            session_id=str(transcript.session_id),
            invoked_by_voice=True,
            correlation_id=transcript.session_id,
        )

    async def _finish_voice_without_text_request(
        self,
        correlation_id: UUID,
        response: Response,
        *,
        outcome: str,
    ) -> tuple[Response, ExecutionOutcome]:
        started = self._voice_interaction_starts.pop(correlation_id, time.perf_counter())
        self._metric_request_starts[correlation_id] = started
        try:
            result = await self._finish(
                response,
                ExecutionOutcome(),
                invoked_by_voice=True,
                correlation_id=correlation_id,
            )
            if self.metrics is not None:
                self.metrics.record(
                    correlation_id=str(correlation_id),
                    stage=MetricStage.REQUEST_TOTAL,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    outcome=outcome,
                    dimensions=MetricDimensions(interaction=InteractionMode.VOICE),
                )
            return result
        finally:
            self._metric_request_starts.pop(correlation_id, None)
            self._response_visible_recorded.discard(correlation_id)

    def _print_voice_gate_debug(
        self,
        transcript: Transcript,
        intent: Intent | None,
        *,
        uncertain: bool,
        sensitive: bool,
        low_words: tuple[WordTimestamp, ...],
        sensitive_argument_keys: tuple[str, ...],
    ) -> None:
        if not self.terminal_debug:
            return
        lines = [
            "[assistant voice debug]",
            f"session_id={transcript.session_id}",
            "gate=sensitive_target_confidence",
            "decision=clarification",
            f"raw_text={AuditStore.redact(transcript.raw_text)}",
            f"normalized_text={AuditStore.redact(transcript.normalized_text)}",
            f"transcript_confidence={transcript.confidence}",
            "transcript_threshold=0.75",
            "word_threshold=0.65",
            f"uncertain={uncertain}",
            f"sensitive={sensitive}",
            f"intent={intent.name if intent else None}",
            "intent_arguments="
            + json.dumps(
                AuditStore.redact(intent.arguments if intent else {}),
                sort_keys=True,
                default=str,
            ),
            "sensitive_argument_keys="
            + json.dumps(sensitive_argument_keys, sort_keys=True, default=str),
            "low_confidence_words="
            + json.dumps(
                [
                    {
                        "word": word.word,
                        "confidence": word.confidence,
                        "start_seconds": word.start_seconds,
                        "end_seconds": word.end_seconds,
                    }
                    for word in low_words
                ],
                sort_keys=True,
                default=str,
            ),
        ]
        print("\n".join(lines), file=sys.stderr, flush=True)

    async def _finish(
        self,
        response: Response,
        outcome: ExecutionOutcome,
        *,
        invoked_by_voice: bool,
        correlation_id: UUID | None = None,
    ) -> tuple[Response, ExecutionOutcome]:
        if correlation_id is not None and correlation_id not in self._response_visible_recorded:
            self._response_visible_recorded.add(correlation_id)
            request_started = self._metric_request_starts.get(correlation_id)
            if self.metrics is not None and request_started is not None:
                self.metrics.record(
                    correlation_id=str(correlation_id),
                    stage=MetricStage.RESPONSE_FIRST_VISIBLE,
                    duration_ms=(time.perf_counter() - request_started) * 1000,
                    outcome=response.response_type.value,
                    dimensions=MetricDimensions(
                        interaction=(
                            InteractionMode.VOICE if invoked_by_voice else InteractionMode.TYPED
                        )
                    ),
                )
            await self.events.publish(
                PipelineEvent(
                    correlation_id=correlation_id,
                    state=(
                        PipelineState.SPEAKING
                        if response.speak and response.speech_text
                        else PipelineState.IDLE
                    ),
                    message="Response ready",
                    data={"response": response.model_dump(mode="json")},
                    updates_foreground_status=False,
                )
            )
        await self._speak_if_enabled(
            response,
            invoked_by_voice=invoked_by_voice,
            correlation_id=correlation_id,
        )
        return response, outcome

    async def _speak_if_enabled(
        self,
        response: Response,
        *,
        invoked_by_voice: bool,
        correlation_id: UUID | None = None,
    ) -> None:
        if self.tts is None or self.speech_mode == "never":
            return
        if self.speech_mode == "voice_only" and not invoked_by_voice:
            return
        started = time.perf_counter()
        first_audio_recorded = False
        first_audio_at: float | None = None
        metric_dimensions = MetricDimensions(
            interaction=InteractionMode.VOICE if invoked_by_voice else InteractionMode.TYPED,
            path=RequestPath.SPEECH,
            provider=self.tts.__class__.__name__,
        )
        try:
            async for event in self.tts.speak(response):
                if (
                    not first_audio_recorded
                    and self.metrics is not None
                    and correlation_id is not None
                ):
                    first_audio_at = time.perf_counter()
                    self.metrics.record(
                        correlation_id=str(correlation_id),
                        stage=MetricStage.TTS_FIRST_AUDIO,
                        duration_ms=(first_audio_at - started) * 1000,
                        outcome="completed",
                        dimensions=metric_dimensions,
                    )
                    self.metrics.record(
                        correlation_id=str(correlation_id),
                        stage=MetricStage.TTS_SYNTHESIS,
                        duration_ms=(first_audio_at - started) * 1000,
                        outcome="completed",
                        dimensions=metric_dimensions,
                    )
                    first_audio_recorded = True
                await self.events.publish(event)
        except Exception:
            # Text is already available and remains the guaranteed fallback.
            if self.metrics is not None and correlation_id is not None:
                self.metrics.record(
                    correlation_id=str(correlation_id),
                    stage=MetricStage.TTS_COMPLETE,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    outcome="failed",
                    dimensions=metric_dimensions,
                )
            return
        if self.metrics is not None and correlation_id is not None:
            completed_at = time.perf_counter()
            if first_audio_at is not None:
                self.metrics.record(
                    correlation_id=str(correlation_id),
                    stage=MetricStage.TTS_PLAYBACK,
                    duration_ms=(completed_at - first_audio_at) * 1000,
                    outcome="completed",
                    dimensions=metric_dimensions,
                )
            self.metrics.record(
                correlation_id=str(correlation_id),
                stage=MetricStage.TTS_COMPLETE,
                duration_ms=(completed_at - started) * 1000,
                outcome="completed",
                dimensions=metric_dimensions,
            )

    async def transcribe_once(
        self,
        stt: STTProvider,
        audio,
        *,
        publish_events: bool = True,
        on_transcript: Callable[[Transcript], None] | None = None,
    ) -> Transcript | None:
        session = AudioSession(source="microphone")
        self._active_stt = stt
        correlation_id = session.session_id
        self._active_correlation = correlation_id
        transcription_started = time.perf_counter()
        self._voice_interaction_starts = {
            key: started
            for key, started in self._voice_interaction_starts.items()
            if transcription_started - started < 300
        }
        self._voice_interaction_starts[correlation_id] = transcription_started
        first_partial_recorded = False
        first_audio_recorded = False
        end_of_utterance_at: float | None = None
        metric_dimensions = MetricDimensions(
            interaction=InteractionMode.VOICE,
            path=RequestPath.SPEECH,
            provider=stt.__class__.__name__,
            warm=getattr(stt, "_model", None) is not None,
        )
        if self.metrics is not None:
            self.metrics.record(
                correlation_id=str(correlation_id),
                stage=MetricStage.ACTIVATION_RECEIVED,
                duration_ms=0,
                outcome="completed",
                dimensions=metric_dimensions,
            )

        async def measured_audio():
            nonlocal first_audio_recorded, end_of_utterance_at
            completed = False
            try:
                async for block in audio:
                    if not first_audio_recorded:
                        first_audio_recorded = True
                        if self.metrics is not None:
                            self.metrics.record(
                                correlation_id=str(correlation_id),
                                stage=MetricStage.AUDIO_FIRST_BLOCK,
                                duration_ms=(time.perf_counter() - transcription_started) * 1000,
                                outcome="completed",
                                dimensions=metric_dimensions,
                            )
                    yield block
                completed = True
            finally:
                end_of_utterance_at = time.perf_counter()
                if self.metrics is not None:
                    self.metrics.record(
                        correlation_id=str(correlation_id),
                        stage=MetricStage.AUDIO_END_OF_UTTERANCE,
                        duration_ms=(end_of_utterance_at - transcription_started) * 1000,
                        outcome="completed" if completed else "cancelled",
                        dimensions=metric_dimensions,
                    )

        if publish_events:
            await self.events.publish(
                PipelineEvent(
                    correlation_id=correlation_id,
                    state=PipelineState.LISTENING,
                    message="Listening",
                )
            )
            if self.metrics is not None:
                self.metrics.record(
                    correlation_id=str(correlation_id),
                    stage=MetricStage.LISTENING_VISIBLE,
                    duration_ms=(time.perf_counter() - transcription_started) * 1000,
                    outcome="completed",
                    dimensions=metric_dimensions,
                )
        final = None
        last_non_empty = None
        try:
            async for transcript in stt.transcribe(session, measured_audio()):
                if transcript.normalized_text.strip():
                    last_non_empty = transcript
                if transcript.is_final:
                    final = transcript
                    if self.metrics is not None:
                        self.metrics.record(
                            correlation_id=str(correlation_id),
                            stage=MetricStage.STT_FINAL,
                            duration_ms=(
                                time.perf_counter() - (end_of_utterance_at or transcription_started)
                            )
                            * 1000,
                            value=transcript.confidence,
                            outcome="completed",
                            dimensions=metric_dimensions,
                        )
                elif not first_partial_recorded:
                    first_partial_recorded = True
                    if self.metrics is not None:
                        self.metrics.record(
                            correlation_id=str(correlation_id),
                            stage=MetricStage.STT_FIRST_PARTIAL,
                            duration_ms=(time.perf_counter() - transcription_started) * 1000,
                            value=transcript.confidence,
                            outcome="completed",
                            dimensions=metric_dimensions,
                        )
                if on_transcript is not None:
                    on_transcript(transcript)
                if publish_events:
                    await self.events.publish(
                        PipelineEvent(
                            correlation_id=correlation_id,
                            state=PipelineState.TRANSCRIBING,
                            message=transcript.raw_text,
                            data={"final": transcript.is_final},
                        )
                    )
                if self.metrics and transcript.confidence is not None:
                    self.metrics.record(
                        correlation_id=str(correlation_id),
                        stage="stt_confidence",
                        value=transcript.confidence,
                        outcome="final" if transcript.is_final else "partial",
                        dimensions=metric_dimensions,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Speech recognition failed",
                extra={
                    "correlation_id": str(correlation_id),
                    "stage": "stt",
                    "outcome": "failed",
                },
            )
            raise RuntimeError(f"Speech recognition is unavailable: {exc}") from exc
        finally:
            self._active_stt = None
        result = final or last_non_empty
        if result is None:
            self._voice_interaction_starts.pop(correlation_id, None)
        return result

    async def listen_once(self, stt: STTProvider, audio) -> tuple[Response, ExecutionOutcome]:
        try:
            final = await self.transcribe_once(stt, audio)
        except RuntimeError as exc:
            response = Response(
                display_text=str(exc),
                speech_text=None,
                response_type=ResponseType.ERROR,
                severity="error",
            )
            return await self._finish(response, ExecutionOutcome(), invoked_by_voice=False)
        if final is None:
            response = Response(
                display_text="No speech was detected.",
                speech_text=None,
                response_type=ResponseType.STATUS,
                severity="warning",
                updates_display=False,
            )
            return await self._finish(response, ExecutionOutcome(), invoked_by_voice=True)
        return await self.handle_transcript(final)

    async def cancel(self, *, exclude_correlation: UUID | None = None) -> None:
        # Provider cancellation is coordinated by the desktop/bootstrap layer.
        self._pending_confirmation = None
        self._replanning_continuations.clear()
        with self._request_state_lock:
            active_requests = tuple(self._active_request_correlations)
        request_correlation = next(
            (
                correlation_id
                for correlation_id in reversed(active_requests)
                if correlation_id != exclude_correlation
            ),
            None,
        )
        cancellation_correlation = request_correlation or self._active_correlation
        await self.engine.cancel(cancellation_correlation)
        if self._active_stt:
            await self._active_stt.cancel()
        if self.planner:
            await self.planner.cancel()
        if self.tts:
            await self.tts.cancel()
        if cancellation_correlation:
            await self.events.publish(
                PipelineEvent(
                    correlation_id=cancellation_correlation,
                    state=PipelineState.CANCELLED,
                    message="Cancelled",
                )
            )
