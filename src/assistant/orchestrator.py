from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import urllib.error
from collections import deque
from collections.abc import Callable
from uuid import UUID, uuid4

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
from assistant.providers import STTProvider
from assistant.providers import TTSProvider
from assistant.events import EventBus
from assistant.execution.engine import ExecutionEngine, ExecutionOutcome
from assistant.language.planner import (
    InvalidPlannerResponse,
    LocalPlanner,
    PlannedIntentStep,
    PlannerDecision,
)
from assistant.response import response_from_result
from assistant.router.deterministic import DeterministicRouter
from assistant.storage.history import HistoryStore
from assistant.storage.database import MetricsStore

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
        self._active_correlation: UUID | None = None
        self._active_stt: STTProvider | None = None
        self._session_context: deque[dict[str, str]] = deque(maxlen=6)

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
            response, final_outcome, invoked_by_voice=invoked_by_voice
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
                    "arguments="
                    + json.dumps(call.arguments, sort_keys=True, default=str),
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
            else " The local model returned an invalid planner response. "
            "Please try again."
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
                    f" for {', '.join(request.scope.targets)}"
                    if request.scope.targets
                    else ""
                )
                permissions.append(
                    f"- {request.scope.capability}{target_text}: "
                    f"{request.scope.reason}"
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
                    f" for {', '.join(request.scope.targets)}"
                    if request.scope.targets
                    else ""
                )
                descriptions.append(
                    f"- {request.scope.capability}{target_text}: "
                    f"{request.scope.reason}"
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

    def _with_reasoning_steps(
        self,
        response: Response,
        *,
        request_text: str,
        intent: Intent | None,
        outcome_label: str,
        step_summaries: tuple[str, ...] = (),
    ) -> Response:
        if not self.show_reasoning_steps:
            return response
        lines = [
            "",
            "Request breakdown:",
            f"- Request: {request_text}",
        ]
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
                lines.append(
                    "- Ambiguity: " + "; ".join(intent.ambiguity_reasons)
                )
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
        route_started = time.perf_counter()
        route = self.router.route(text)
        route_duration = (time.perf_counter() - route_started) * 1000
        intent = route.intent
        self._record_routing(correlation_id, intent, route_duration)
        if route.clarification:
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
                if hasattr(self.planner, "plan_decision"):
                    decision = await self.planner.plan_decision(
                        text, tuple(self._session_context)
                    )
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
        if intent.name == "control.forget":
            self._session_context.clear()
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
            )
            return await self._finish_interaction(
                correlation_id,
                text,
                response,
                intent_name="planner.empty",
                outcome_label="failed",
                invoked_by_voice=invoked_by_voice,
            )
        return await self._handle_planner_intent(
            correlation_id,
            text,
            decision.intent,
            session_id=session_id,
            invoked_by_voice=invoked_by_voice,
        )

    async def _handle_planner_intent(
        self,
        correlation_id: UUID,
        text: str,
        intent: Intent,
        *,
        session_id: str,
        invoked_by_voice: bool,
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
            return await self._finish(
                response, ExecutionOutcome(), invoked_by_voice=True
            )
        route = self.router.route(transcript.normalized_text)
        intent = route.intent
        low_word = any(
            word.confidence is not None and word.confidence < 0.65
            for word in transcript.words
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
        uncertain = (
            transcript.confidence is not None and transcript.confidence < 0.75
        ) or low_word
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
                    sorted(sensitive_keys.intersection(intent.arguments))
                    if intent
                    else ()
                ),
            )
            response = Response(
                display_text=(
                    "I am not confident I heard the sensitive target correctly. "
                    "Please repeat the exact path, process, or device."
                ),
                speech_text=(
                    "Please repeat the exact target. I did not perform the action."
                ),
                response_type=ResponseType.CLARIFICATION,
                severity="warning",
                speak=True,
            )
            return await self._finish(
                response, ExecutionOutcome(), invoked_by_voice=True
            )
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
            f"word_threshold=0.65",
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

    async def _speak_if_enabled(
        self, response: Response, *, invoked_by_voice: bool
    ) -> None:
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

    async def listen_once(
        self, stt: STTProvider, audio
    ) -> tuple[Response, ExecutionOutcome]:
        try:
            final = await self.transcribe_once(stt, audio)
        except RuntimeError as exc:
            response = Response(
                display_text=str(exc),
                speech_text=None,
                response_type=ResponseType.ERROR,
                severity="error",
            )
            return await self._finish(
                response, ExecutionOutcome(), invoked_by_voice=False
            )
        if final is None:
            response = Response(
                display_text="No speech was detected.",
                speech_text="I did not hear anything.",
                response_type=ResponseType.STATUS,
                severity="warning",
            )
            return await self._finish(
                response, ExecutionOutcome(), invoked_by_voice=True
            )
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
