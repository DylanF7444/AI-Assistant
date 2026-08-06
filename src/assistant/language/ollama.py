from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OllamaUsage:
    correlation_id: str | None
    operation: str
    wall_duration_ms: float
    total_duration_ms: float
    load_duration_ms: float
    prompt_eval_duration_ms: float
    generation_duration_ms: float
    prompt_tokens: int | None
    output_tokens: int | None


class OllamaProvider:
    def __init__(
        self,
        model: str,
        endpoint: str = "http://127.0.0.1:11434/api/generate",
        max_tokens: int = 256,
        max_prompt_chars: int = 12000,
        terminal_debug: bool = False,
        keep_alive: str = "30m",
        usage_callback: Callable[[OllamaUsage], None] | None = None,
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.max_tokens = max_tokens
        self.max_prompt_chars = max_prompt_chars
        self.terminal_debug = terminal_debug
        self.keep_alive = keep_alive
        self.usage_callback = usage_callback
        self._task: asyncio.Task | None = None
        self._server_process: subprocess.Popen | None = None
        self._preload_thread: threading.Thread | None = None
        self._correlation_id = contextvars.ContextVar[str | None](
            f"ollama_correlation_{id(self)}",
            default=None,
        )
        self._operation = contextvars.ContextVar[str](
            f"ollama_operation_{id(self)}",
            default="completion",
        )
        self._token_budget = contextvars.ContextVar[int | None](
            f"ollama_token_budget_{id(self)}",
            default=None,
        )

    @contextmanager
    def metric_scope(self, correlation_id: str) -> Iterator[None]:
        token = self._correlation_id.set(correlation_id)
        try:
            yield
        finally:
            self._correlation_id.reset(token)

    @contextmanager
    def operation_scope(self, operation: str) -> Iterator[None]:
        token = self._operation.set(operation)
        try:
            yield
        finally:
            self._operation.reset(token)

    @contextmanager
    def token_budget_scope(self, max_tokens: int) -> Iterator[None]:
        token = self._token_budget.set(max(1, int(max_tokens)))
        try:
            yield
        finally:
            self._token_budget.reset(token)

    def _bounded_prompt(self, prompt: str) -> str:
        if len(prompt) <= self.max_prompt_chars:
            return prompt
        marker = "\n[Earlier planner context omitted for local inference.]\n"
        tail_chars = max(1, self.max_prompt_chars // 3)
        head_chars = self.max_prompt_chars - tail_chars - len(marker)
        return f"{prompt[:head_chars]}{marker}{prompt[-tail_chars:]}"

    def _print_completion_debug(self, prompt: str, response: str) -> None:
        if not self.terminal_debug:
            return
        print(
            "\n".join(
                (
                    "[assistant ai debug]",
                    f"model={self.model}",
                    "prompt:",
                    prompt,
                    "response:",
                    response,
                )
            ),
            file=sys.stderr,
            flush=True,
        )

    @staticmethod
    def _duration_ms(body: dict[str, Any], key: str) -> float:
        value = body.get(key)
        return float(value) / 1_000_000 if isinstance(value, int | float) else 0.0

    def _publish_usage(
        self,
        body: dict[str, Any],
        *,
        operation: str,
        wall_duration_ms: float,
    ) -> None:
        if self.usage_callback is None:
            return
        usage = OllamaUsage(
            correlation_id=self._correlation_id.get(),
            operation=operation,
            wall_duration_ms=wall_duration_ms,
            total_duration_ms=self._duration_ms(body, "total_duration"),
            load_duration_ms=self._duration_ms(body, "load_duration"),
            prompt_eval_duration_ms=self._duration_ms(body, "prompt_eval_duration"),
            generation_duration_ms=self._duration_ms(body, "eval_duration"),
            prompt_tokens=(
                int(body["prompt_eval_count"])
                if isinstance(body.get("prompt_eval_count"), int | float)
                else None
            ),
            output_tokens=(
                int(body["eval_count"])
                if isinstance(body.get("eval_count"), int | float)
                else None
            ),
        )
        try:
            self.usage_callback(usage)
        except Exception:
            logger.warning("Could not record Ollama usage metrics", exc_info=True)

    def _send(
        self,
        request_payload: dict[str, Any],
        *,
        timeout_seconds: float,
        operation: str,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        data = json.dumps(request_payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
            body = json.load(response)
        self._publish_usage(
            body,
            operation=operation,
            wall_duration_ms=(time.perf_counter() - started) * 1000,
        )
        return body

    def _start_local_server(self) -> bool:
        executable = shutil.which("ollama")
        if executable is None:
            return False
        startupinfo = None
        creationflags = 0
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._server_process = subprocess.Popen(
            [executable, "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        for _ in range(30):
            if self._server_process.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(
                    self.endpoint.rsplit("/api/", 1)[0] + "/api/tags",
                    timeout=0.25,
                ):
                    return True
            except Exception:
                time.sleep(0.1)
        return False

    async def structured_completion(
        self, prompt: str, schema: dict[str, Any], timeout_seconds: float = 15
    ) -> dict[str, Any]:
        bounded_prompt = self._bounded_prompt(prompt)
        token_budget = self._token_budget.get() or self.max_tokens
        payload = {
            "model": self.model,
            "prompt": bounded_prompt,
            "stream": False,
            "think": False,
            "format": schema,
            "keep_alive": self.keep_alive,
            "options": {"num_predict": token_budget, "temperature": 0},
        }

        def request() -> dict[str, Any]:
            operation = self._operation.get()
            try:
                body = self._send(
                    payload,
                    timeout_seconds=timeout_seconds,
                    operation=operation,
                )
            except urllib.error.URLError:
                if not self._start_local_server():
                    raise
                body = self._send(
                    payload,
                    timeout_seconds=timeout_seconds,
                    operation=operation,
                )
            raw_response = body["response"]
            self._print_completion_debug(payload["prompt"], raw_response)
            try:
                result = json.loads(raw_response)
            except json.JSONDecodeError:
                retry_payload = {
                    **payload,
                    "prompt": self._bounded_prompt(
                        bounded_prompt
                        + "\nYour previous response was truncated or malformed. Return one "
                        "complete, concise JSON object matching the schema. Keep answer and "
                        "clarification text under 200 characters. Do not include commentary, "
                        "emoji, or hashtags. When a listed skill performs the action, use the "
                        "skill disposition and set answer to null."
                    ),
                    "options": {
                        **payload["options"],
                        "num_predict": max(token_budget * 2, 512),
                    },
                }
                body = self._send(
                    retry_payload,
                    timeout_seconds=timeout_seconds,
                    operation=f"{operation}.repair",
                )
                raw_response = body["response"]
                self._print_completion_debug(retry_payload["prompt"], raw_response)
                result = json.loads(raw_response)
            TypeAdapter(dict[str, Any]).validate_python(result)
            return result

        self._task = asyncio.create_task(asyncio.to_thread(request))
        try:
            return await asyncio.wait_for(self._task, timeout=timeout_seconds)
        finally:
            self._task = None

    async def preload(self, timeout_seconds: float = 15) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
        }

        def request() -> dict[str, Any]:
            try:
                return self._send(
                    payload,
                    timeout_seconds=timeout_seconds,
                    operation="preload",
                )
            except urllib.error.URLError:
                if not self._start_local_server():
                    raise
                return self._send(
                    payload,
                    timeout_seconds=timeout_seconds,
                    operation="preload",
                )

        return await asyncio.wait_for(
            asyncio.to_thread(request),
            timeout=timeout_seconds,
        )

    def preload_in_background(self, timeout_seconds: float = 15) -> None:
        if self._preload_thread is not None and self._preload_thread.is_alive():
            return

        def run() -> None:
            try:
                asyncio.run(self.preload(timeout_seconds=timeout_seconds))
            except Exception:
                logger.debug("Ollama background preload did not complete", exc_info=True)

        self._preload_thread = threading.Thread(
            target=run,
            name="ollama-preload",
            daemon=True,
        )
        self._preload_thread.start()

    async def cancel(self) -> None:
        if self._task:
            self._task.get_loop().call_soon_threadsafe(self._task.cancel)

    async def health(self) -> dict[str, Any]:
        try:
            await self.structured_completion(
                'Return {"ok": true}.',
                {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                timeout_seconds=15,
            )
            return {"healthy": True, "model": self.model}
        except Exception as exc:
            error = str(exc).strip() or type(exc).__name__
            return {"healthy": False, "model": self.model, "error": error}
