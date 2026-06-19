from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from pydantic import TypeAdapter, ValidationError


class OllamaProvider:
    def __init__(
        self,
        model: str,
        endpoint: str = "http://127.0.0.1:11434/api/generate",
        max_tokens: int = 256,
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.max_tokens = max_tokens
        self._task: asyncio.Task | None = None
        self._server_process: subprocess.Popen | None = None

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
        payload = {
            "model": self.model,
            "prompt": prompt[-8000:],
            "stream": False,
            "format": schema,
            "options": {"num_predict": self.max_tokens, "temperature": 0},
        }

        def request() -> dict[str, Any]:
            data = json.dumps(payload).encode("utf-8")
            def send() -> dict[str, Any]:
                req = urllib.request.Request(
                    self.endpoint,
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(
                    req, timeout=timeout_seconds
                ) as response:
                    return json.load(response)

            try:
                body = send()
            except urllib.error.URLError:
                if not self._start_local_server():
                    raise
                body = send()
            result = json.loads(body["response"])
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
