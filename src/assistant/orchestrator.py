from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import urllib.error
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

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
    AudioSession,
    ExecutionPlan,
    Intent,
    PermissionContext,
    PipelineEvent,
    PipelineState,
    PlanStep,
    Response,
    ResponseType,
    SkillCall,
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
)
from assistant.policy.permissions import GrantType
from assistant.providers import STTProvider, TTSProvider
from assistant.response import response_from_result
from assistant.router.deterministic import DeterministicRouter
from assistant.storage.database import MetricsStore
from assistant.storage.history import HistoryStore
from assistant.storage.knowledge import KnowledgeBaseStore
from assistant.storage.memory import MemoryStore
from assistant.windows.paths import (
    ASSISTANT_FOLDER_TOKEN,
    KNOWN_USER_FOLDERS,
    resolve_user_path,
)

logger = logging.getLogger("assistant.orchestrator")


@dataclass(frozen=True)
class PendingClarificationFollowUp:
    original_text: str
    clarification: str
    folder_name: str | None
    session_id: str
    kind: str = "generic"


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
        context_providers: tuple[ContextProvider, ...] = (),
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
        self.context_providers = context_providers
        self._active_correlation: UUID | None = None
        self._active_stt: STTProvider | None = None
        self._session_context: deque[dict[str, str]] = deque(maxlen=6)
        self._file_candidates: tuple[str, ...] = ()
        self._selected_file: str | None = None
        self._pending_clarification_follow_up: PendingClarificationFollowUp | None = None

    def remember_file_candidates(self, paths: tuple[str, ...] | list[str]) -> None:
        self._file_candidates = tuple(str(path) for path in paths)
        if len(self._file_candidates) == 1:
            self._selected_file = self._file_candidates[0]

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
        self.history.add(
            correlation_id,
            response.display_text,
            raw_transcript=request_text,
            normalized_transcript=request_text.casefold(),
            intent_name=intent_name,
        )
        if not add_session_context:
            return
        self._session_context.append(
            {
                "request": request_text,
                "intent": intent_name,
                "outcome": outcome,
                "response_type": response.response_type.value,
                "severity": response.severity,
                "response": response.display_text[:1000],
            }
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
        if final_outcome.result:
            self._remember_file_result(final_outcome.result)
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
        return await self._finish(response, final_outcome, invoked_by_voice=invoked_by_voice)

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
            f"request={request_text}",
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
                    "arguments=" + json.dumps(call.arguments, sort_keys=True, default=str),
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

    def _record_routing(
        self, correlation_id: UUID, intent: Intent | None, duration_ms: float
    ) -> None:
        if self.metrics:
            self.metrics.record(
                correlation_id=str(correlation_id),
                stage="routing",
                duration_ms=duration_ms,
                value=intent.confidence if intent else None,
                outcome=intent.name if intent else "clarification",
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
    ) -> ContextSnapshot:
        providers = [
            SessionContextProvider(
                recent_context=lambda: tuple(self._session_context),
                pending_clarification=self._pending_clarification_context,
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
        text: str,
        *,
        session_id: str,
    ) -> PlannerDecision | None:
        assert self.planner is not None
        retrieved = await self._retrieve_planner_goal_context(text)
        context = await self._context_snapshot_for_planner_goal(
            text,
            session_id=session_id,
        )
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

    async def _retrieve_planner_goal_context(
        self,
        text: str,
    ) -> tuple[RetrievedContextItem, ...]:
        if self.semantic_retriever is None:
            return ()
        try:
            if self.skill_semantic_indexer is not None:
                await self.skill_semantic_indexer.refresh(self.engine.registry)
            if self.knowledge_semantic_indexer is not None and self.knowledge_bases is not None:
                await self.knowledge_semantic_indexer.refresh(self.knowledge_bases)
            if self.memory_semantic_indexer is not None and self.memory_store is not None:
                await self.memory_semantic_indexer.refresh(self.memory_store)
            if self.workflow_template_indexer is not None:
                await self.workflow_template_indexer.refresh()
            return await self.semantic_retriever.search(
                text,
                namespaces={
                    "knowledge",
                    "memory",
                    "skill",
                    "skill_example",
                    "workflow_template",
                },
                limit=8,
            )
        except Exception as exc:
            logger.warning("Semantic retrieval failed", exc_info=exc)
            return ()

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
    def _retrieved_debug_item(item: Any) -> dict[str, Any]:
        metadata = getattr(item, "metadata", {})
        safe_metadata = {}
        if isinstance(metadata, dict):
            for key in (
                "skill_id",
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

    def _remember_file_result(self, result: Any) -> None:
        paths = result.data.get("paths") if isinstance(result.data, dict) else None
        if isinstance(paths, list) and all(isinstance(path, str) for path in paths):
            self.remember_file_candidates(tuple(paths))

    def _select_file_candidate(self, selector: str) -> Response:
        if not self._file_candidates:
            return Response(
                display_text="There are no file search results to select from.",
                speech_text="There are no file results to select from.",
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )
        index = self._selection_index(selector)
        if index == -1:
            index = len(self._file_candidates) - 1
        if index is None or index < 0 or index >= len(self._file_candidates):
            return Response(
                display_text=('Select one of the numbered file results, for example "select 1".'),
                speech_text="Please select one of the numbered file results.",
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )
        self._selected_file = self._file_candidates[index]
        return Response(
            display_text=f"Selected file:\n{self._selected_file}",
            speech_text="Selected.",
            response_type=ResponseType.STATUS,
            severity="success",
            speak=True,
        )

    @staticmethod
    def _selection_index(selector: str) -> int | None:
        value = selector.strip().casefold()
        value = re.sub(r"^(?:the|that|this)\s+", "", value)
        value = re.sub(r"\s+(?:one|file|item|result|match)$", "", value).strip()
        value = re.sub(r"^(?:file|item|result|match|number)\s+", "", value).strip()
        words = {
            "first": 1,
            "one": 1,
            "second": 2,
            "two": 2,
            "third": 3,
            "three": 3,
            "fourth": 4,
            "four": 4,
            "fifth": 5,
            "five": 5,
            "last": -1,
        }
        if value in words:
            number = words[value]
            return number - 1 if number > 0 else -1
        if match := re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", value):
            return int(match.group(1)) - 1
        return None

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

        if pending.kind == "folder_search":
            return await self._handle_pending_folder_search_follow_up(
                correlation_id,
                text,
                pending,
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
        return (
            f"{pending.original_text}\n"
            f"Clarifying question: {pending.clarification}\n"
            f"Follow-up answer: {answer}\n"
            "Use the follow-up answer to complete the previous request."
        )

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
    ) -> tuple[Response, ExecutionOutcome]:
        correlation_id = uuid4()
        self._active_correlation = correlation_id
        await self.events.publish(
            PipelineEvent(
                correlation_id=correlation_id,
                state=PipelineState.THINKING,
                message="Interpreting command",
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
        route_started = time.perf_counter()
        route = self.router.route(text)
        route_duration = (time.perf_counter() - route_started) * 1000
        intent = route.intent
        self._record_routing(correlation_id, intent, route_duration)
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
            if self.planner is None:
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
            try:
                if (
                    intent is not None
                    and intent.name == "planner.goal"
                    and hasattr(self.planner, "interpret")
                ):
                    decision = await self._interpret_planner_goal(
                        text,
                        session_id=session_id,
                    )
                    if decision is not None:
                        return await self._handle_planner_decision(
                            correlation_id,
                            text,
                            decision,
                            session_id=session_id,
                            invoked_by_voice=invoked_by_voice,
                        )
                if hasattr(self.planner, "plan_decision"):
                    decision = await self.planner.plan_decision(text, tuple(self._session_context))
                    return await self._handle_planner_decision(
                        correlation_id,
                        text,
                        decision,
                        session_id=session_id,
                        invoked_by_voice=invoked_by_voice,
                    )
                intent = await self.planner.plan(text, tuple(self._session_context))
            except Exception as exc:
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
            await self.cancel()
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
            self._session_context.clear()
            self._file_candidates = ()
            self._selected_file = None
            self._pending_clarification_follow_up = None
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
        if intent.name == "control.file_select":
            response = self._select_file_candidate(intent.arguments["selector"])
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
        resolved_intent = self._resolve_file_placeholders(
            intent,
            session_id=session_id,
            request_text=text,
        )
        if isinstance(resolved_intent, Response):
            response = self._with_reasoning_steps(
                resolved_intent,
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
        intent = resolved_intent
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
    ) -> tuple[Response, ExecutionOutcome]:
        assert self.planner is not None
        observations: list[dict[str, Any]] = []
        current_intent = intent
        last_outcome = ExecutionOutcome()
        step_summaries: list[str] = []
        for iteration in range(1, max_iterations + 1):
            call = SkillCall(
                skill_id=current_intent.name,
                caller="planner",
                arguments=current_intent.arguments,
                permission_context=PermissionContext(session_id=session_id),
                correlation_id=correlation_id,
            )
            try:
                outcome = await self.engine.prepare(call)
            except Exception as exc:
                logger.warning("Replanned command preparation failed", exc_info=exc)
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
            step_summaries.append(
                f"Prepare {current_intent.name} with "
                f"{json.dumps(current_intent.arguments, default=str)}"
            )
            outcome_label = self._outcome_label(outcome)
            observations.append(
                self._observation_from_outcome(
                    current_intent,
                    outcome,
                    iteration=iteration,
                    outcome_label=outcome_label,
                )
            )
            if (
                outcome.confirmation
                or outcome.permission_requests
                or outcome.registration_requests
                or outcome.result is None
                or outcome.result.status.value != "completed"
            ):
                response = self._response_from_outcome(outcome)
                response = self._with_reasoning_steps(
                    response,
                    request_text=text,
                    intent=current_intent,
                    outcome_label=outcome_label,
                    step_summaries=tuple(step_summaries),
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
            revision = await self.planner.revise_plan(
                text,
                interpretation=interpretation,
                context=context,
                retrieved=retrieved,
                observations=tuple(observations),
            )
            if revision.disposition == "next_step":
                current_intent = Intent(
                    name=revision.skill_id,
                    arguments=revision.arguments,
                    confidence=revision.confidence,
                    source="llm",
                )
                continue
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
            response = Response(
                display_text=revision.response or last_outcome.result.summary,
                speech_text=revision.response or last_outcome.result.summary,
                response_type=(
                    ResponseType.ERROR if revision.disposition == "reject" else ResponseType.STATUS
                ),
                severity="warning" if revision.disposition == "reject" else "success",
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
                speech_text="I did not hear anything.",
                response_type=ResponseType.STATUS,
                severity="warning",
            )
            return await self._finish(response, ExecutionOutcome(), invoked_by_voice=True)
        route = self.router.route(transcript.normalized_text)
        intent = route.intent
        low_word = any(
            word.confidence is not None and word.confidence < 0.65 for word in transcript.words
        )
        low_words = tuple(
            word
            for word in transcript.words
            if word.confidence is not None and word.confidence < 0.65
        )
        sensitive_keys = {
            "path",
            "source",
            "destination",
            "recipient",
            "amount",
            "device_id",
            "pid",
            "process_name",
            "entity_id",
        }
        uncertain = (transcript.confidence is not None and transcript.confidence < 0.75) or low_word
        sensitive = bool(
            intent
            and (
                sensitive_keys.intersection(intent.arguments)
                or intent.name
                in {
                    "windows.file_operation",
                    "windows.process",
                    "windows.input_automation",
                    "integration.device_control",
                    "integration.home_assistant",
                }
            )
        )
        if uncertain and sensitive:
            self._print_voice_gate_debug(
                transcript,
                intent,
                uncertain=uncertain,
                sensitive=sensitive,
                low_words=low_words,
                sensitive_argument_keys=tuple(
                    sorted(sensitive_keys.intersection(intent.arguments)) if intent else ()
                ),
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
            return await self._finish(response, ExecutionOutcome(), invoked_by_voice=True)
        return await self.handle_text(
            transcript.normalized_text,
            session_id=str(transcript.session_id),
            invoked_by_voice=True,
        )

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
            f"raw_text={transcript.raw_text}",
            f"normalized_text={transcript.normalized_text}",
            f"transcript_confidence={transcript.confidence}",
            "transcript_threshold=0.75",
            "word_threshold=0.65",
            f"uncertain={uncertain}",
            f"sensitive={sensitive}",
            f"intent={intent.name if intent else None}",
            "intent_arguments="
            + json.dumps(intent.arguments if intent else {}, sort_keys=True, default=str),
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
    ) -> tuple[Response, ExecutionOutcome]:
        await self._speak_if_enabled(response, invoked_by_voice=invoked_by_voice)
        return response, outcome

    async def _speak_if_enabled(self, response: Response, *, invoked_by_voice: bool) -> None:
        if self.tts is None or self.speech_mode == "never":
            return
        if self.speech_mode == "voice_only" and not invoked_by_voice:
            return
        try:
            async for event in self.tts.speak(response):
                await self.events.publish(event)
        except Exception:
            # Text is already available and remains the guaranteed fallback.
            return

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
        if publish_events:
            await self.events.publish(
                PipelineEvent(
                    correlation_id=correlation_id,
                    state=PipelineState.LISTENING,
                    message="Listening",
                )
            )
        final = None
        last_non_empty = None
        try:
            async for transcript in stt.transcribe(session, audio):
                if transcript.normalized_text.strip():
                    last_non_empty = transcript
                if transcript.is_final:
                    final = transcript
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
        return final or last_non_empty

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
                speech_text="I did not hear anything.",
                response_type=ResponseType.STATUS,
                severity="warning",
            )
            return await self._finish(response, ExecutionOutcome(), invoked_by_voice=True)
        return await self.handle_transcript(final)

    async def cancel(self) -> None:
        # Provider cancellation is coordinated by the desktop/bootstrap layer.
        await self.engine.cancel(self._active_correlation)
        if self._active_stt:
            await self._active_stt.cancel()
        if self.planner:
            await self.planner.cancel()
        if self.tts:
            await self.tts.cancel()
        if self._active_correlation:
            await self.events.publish(
                PipelineEvent(
                    correlation_id=self._active_correlation,
                    state=PipelineState.CANCELLED,
                    message="Cancelled",
                )
            )
