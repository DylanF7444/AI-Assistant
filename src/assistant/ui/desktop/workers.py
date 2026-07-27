from __future__ import annotations

import asyncio
from typing import Any

from PySide6.QtCore import QThread, Signal

from assistant.audio.activation import keyword_request
from assistant.bootstrap import Container


class AsyncWorker(QThread):
    complete = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, coroutine: Any) -> None:
        super().__init__()
        self.coroutine = coroutine
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True
        loop = self._loop
        task = self._task
        if loop is not None and task is not None and not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        task = loop.create_task(self.coroutine)
        self._task = task
        if self._cancel_requested:
            task.cancel()
        try:
            self.complete.emit(loop.run_until_complete(task))
        except asyncio.CancelledError:
            self.cancelled.emit()
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            pending = asyncio.all_tasks(loop)
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._task = None
            self._loop = None
            asyncio.set_event_loop(None)
            loop.close()


class PassiveVoiceWorker(QThread):
    complete = Signal(object)
    failed = Signal(str)
    partial_transcript = Signal(str)
    request_transcript = Signal(str)

    def __init__(self, container: Container, mode: str) -> None:
        super().__init__()
        self.container = container
        self.mode = mode
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            return
        except Exception as error:
            self.failed.emit(str(error))

    async def _run(self) -> None:
        while not self._stop_requested:
            if self.mode == "persistent":
                await self._listen_persistent()
            else:
                await self._listen_for_activation()

    async def _listen_persistent(self) -> None:
        transcript = await self.container.orchestrator.transcribe_once(
            self.container.stt,
            self.container.audio_capture.chunks(),
            publish_events=False,
            on_transcript=self._show_transcript,
        )
        if self._stop_requested or transcript is None or not transcript.normalized_text.strip():
            return
        spoken = transcript.normalized_text.strip()
        if await self.container.orchestrator.consume_persistent_voice_interrupt(spoken):
            return
        result = await self.container.orchestrator.handle_transcript(transcript)
        self.request_transcript.emit(transcript.raw_text.strip() or spoken)
        self.complete.emit(result)

    async def _listen_for_activation(self) -> None:
        transcript = await self.container.orchestrator.transcribe_once(
            self.container.stt,
            self.container.audio_capture.chunks(),
            publish_events=False,
            on_transcript=self._show_transcript,
        )
        if self._stop_requested:
            return
        settings = self.container.settings
        clap_detected = (
            settings.voice_activation_clap_enabled
            and self.container.audio_capture.consume_clap_detection()
        )
        request = None
        if settings.voice_activation_keyword_enabled and transcript is not None:
            request = keyword_request(
                transcript.normalized_text,
                settings.voice_activation_keyword,
            )
        if request is None and not clap_detected:
            return
        if request:
            result = await self.container.orchestrator.handle_text(
                request,
                session_id=(
                    str(transcript.session_id) if transcript is not None else "voice-activation"
                ),
                invoked_by_voice=True,
            )
            self.request_transcript.emit(request)
            self.complete.emit(result)
        else:
            self.complete.emit(
                await self.container.orchestrator.listen_once(
                    self.container.stt,
                    self.container.audio_capture.chunks(),
                )
            )

    def _show_transcript(self, transcript: Any) -> None:
        if transcript.raw_text.strip():
            self.partial_transcript.emit(transcript.raw_text)


class VoicePromptWorker(QThread):
    complete = Signal(str)
    failed = Signal(str)
    partial_transcript = Signal(str)

    def __init__(self, container: Container) -> None:
        super().__init__()
        self.container = container
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            return
        except Exception as error:
            self.failed.emit(str(error))

    async def _run(self) -> None:
        while not self._stop_requested:
            transcript = await self.container.orchestrator.transcribe_once(
                self.container.stt,
                self.container.audio_capture.chunks(),
                publish_events=False,
                on_transcript=self._show_transcript,
            )
            if self._stop_requested:
                return
            if transcript is None:
                continue
            self.complete.emit(transcript.normalized_text or transcript.raw_text)

    def _show_transcript(self, transcript: Any) -> None:
        if transcript.raw_text.strip():
            self.partial_transcript.emit(transcript.raw_text)
