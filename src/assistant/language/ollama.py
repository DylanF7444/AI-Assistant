from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from pydantic import TypeAdapter


class OllamaProvider:
    def __init__(
        self,
        model: str,
        endpoint: str = "http://127.0.0.1:11434/api/generate",
        max_tokens: int = 256,
        max_prompt_chars: int = 4500,
        terminal_debug: bool = False,
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.max_tokens = max_tokens
        self.max_prompt_chars = max_prompt_chars
        self.terminal_debug = terminal_debug
        self._task: asyncio.Task | None = None
        self._server_process: subprocess.Popen | None = None

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
        payload = {
            "model": self.model,
            "prompt": bounded_prompt,
            "stream": False,
            "format": schema,
            "options": {"num_predict": self.max_tokens, "temperature": 0},
        }

        def request() -> dict[str, Any]:
            def send(request_payload: dict[str, Any]) -> dict[str, Any]:
                data = json.dumps(request_payload).encode("utf-8")
                req = urllib.request.Request(
                    self.endpoint,
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
                    return json.load(response)

            try:
                body = send(payload)
            except urllib.error.URLError:
                if not self._start_local_server():
                    raise
                body = send(payload)
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
                        "num_predict": max(self.max_tokens * 2, 512),
                    },
                }
                body = send(retry_payload)
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
