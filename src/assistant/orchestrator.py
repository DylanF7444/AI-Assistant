from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
from collections import deque
from uuid import UUID, uuid4

from assistant.contracts import (
    AudioSession,
    PermissionContext,
    PipelineEvent,
    PipelineState,
    Response,
    ResponseType,
    SkillCall,
    Transcript,
)
from assistant.providers import STTProvider
from assistant.providers import TTSProvider
from assistant.events import EventBus
from assistant.execution.engine import ExecutionEngine, ExecutionOutcome
from assistant.language.planner import LocalPlanner
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
        self.allow_sensitive_speech = allow_sensitive_speech
        self.hide_sensitive_display = hide_sensitive_display
        self._active_correlation: UUID | None = None
        self._active_stt: STTProvider | None = None
        self._session_context: deque[dict[str, str]] = deque(maxlen=6)

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
        if self.metrics:
            self.metrics.record(
                correlation_id=str(correlation_id),
                stage="routing",
                duration_ms=route_duration,
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
        if route.clarification:
            response = Response(
                display_text=route.clarification,
                speech_text=route.clarification,
                response_type=ResponseType.CLARIFICATION,
            )
            return await self._finish(
                response, ExecutionOutcome(), invoked_by_voice=invoked_by_voice
            )
        if route.needs_llm:
            if self.planner is None:
                response = Response(
                    display_text="I could not match that to an offline core command.",
                    speech_text="I could not match that to an available command.",
                    response_type=ResponseType.ERROR,
                    severity="warning",
                )
                return await self._finish(
                    response, ExecutionOutcome(), invoked_by_voice=invoked_by_voice
                )
            try:
                intent = await self.planner.plan(
                    text, tuple(self._session_context)
                )
            except Exception as exc:
                logger.warning("Local planner failed", exc_info=exc)
                unavailable = isinstance(
                    exc,
                    (
                        ConnectionError,
                        TimeoutError,
                        urllib.error.URLError,
                    ),
                )
                guidance = (
                    " Start Ollama and verify the selected local model is installed."
                    if unavailable
                    else " The local model returned an invalid planner response. "
                    "Please try again."
                )
                response = Response(
                    display_text=(
                        "I could not use the local planner."
                        f"{guidance} Deterministic commands still work."
                    ),
                    speech_text="The local planner is unavailable.",
                    response_type=ResponseType.ERROR,
                    severity="warning",
                )
                return await self._finish(
                    response, ExecutionOutcome(), invoked_by_voice=invoked_by_voice
                )
        assert intent is not None
        if intent.name in {"conversation.answer", "conversation.reject"}:
            response = Response(
                display_text=intent.arguments["text"],
                speech_text=intent.arguments["text"],
                response_type=ResponseType.ANSWER,
                severity="warning" if intent.name == "conversation.reject" else "info",
            )
            self.history.add(
                correlation_id,
                response.display_text,
                raw_transcript=text,
                normalized_transcript=text.casefold(),
                intent_name=intent.name,
            )
            return await self._finish(
                response, ExecutionOutcome(), invoked_by_voice=invoked_by_voice
            )
        if intent.name == "control.cancel":
            await self.cancel()
            response = Response(
                display_text="Cancelled.",
                speech_text="Cancelled.",
                response_type=ResponseType.CONTROL,
            )
            return await self._finish(
                response, ExecutionOutcome(), invoked_by_voice=invoked_by_voice
            )
        if intent.name == "control.forget":
            self._session_context.clear()
            response = Response(
                display_text="I forgot the current session context.",
                speech_text="Session context cleared.",
                response_type=ResponseType.CONTROL,
            )
            return await self._finish(
                response, ExecutionOutcome(), invoked_by_voice=invoked_by_voice
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
            return await self._finish(
                response, ExecutionOutcome(), invoked_by_voice=invoked_by_voice
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
            response = Response(
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
        elif outcome.confirmation:
            response = Response(
                display_text=outcome.confirmation.exact_action,
                speech_text="Please review and confirm the action shown on screen.",
                response_type=ResponseType.CONFIRMATION,
                severity="warning",
                speak=True,
            )
        elif outcome.result:
            response = response_from_result(
                outcome.result,
                detail=self.response_detail,
                allow_sensitive_speech=self.allow_sensitive_speech,
                hide_sensitive_display=self.hide_sensitive_display,
            )
        else:
            response = Response(
                display_text="No action was performed.",
                response_type=ResponseType.STATUS,
                severity="warning",
            )
        self.history.add(
            correlation_id,
            response.display_text,
            raw_transcript=text,
            normalized_transcript=text.casefold(),
            intent_name=intent.name,
        )
        self._session_context.append(
            {
                "request": text,
                "intent": intent.name,
                "outcome": (
                    outcome.result.status.value
                    if outcome.result
                    else "awaiting_confirmation"
                    if outcome.confirmation
                    else "none"
                ),
            }
        )
        return await self._finish(
            response, outcome, invoked_by_voice=invoked_by_voice
        )

    async def handle_transcript(self, transcript: Transcript) -> tuple[Response, ExecutionOutcome]:
        route = self.router.route(transcript.normalized_text)
        intent = route.intent
        low_word = any(
            word.confidence is not None and word.confidence < 0.65
            for word in transcript.words
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

    async def listen_once(
        self, stt: STTProvider, audio
    ) -> tuple[Response, ExecutionOutcome]:
        session = AudioSession(source="microphone")
        self._active_stt = stt
        correlation_id = session.session_id
        self._active_correlation = correlation_id
        await self.events.publish(
            PipelineEvent(
                correlation_id=correlation_id,
                state=PipelineState.LISTENING,
                message="Listening",
            )
        )
        await self.events.publish(
            PipelineEvent(
                correlation_id=correlation_id,
                state=PipelineState.TRANSCRIBING,
                message="Transcribing locally",
            )
        )
        final = None
        try:
            async for transcript in stt.transcribe(session, audio):
                final = transcript
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
            response = Response(
                display_text=f"Speech recognition is unavailable: {exc}",
                speech_text=None,
                response_type=ResponseType.ERROR,
                severity="error",
            )
            return await self._finish(
                response, ExecutionOutcome(), invoked_by_voice=False
            )
        finally:
            self._active_stt = None
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
