from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from jsonschema import validate

from assistant.contracts import SkillCall, SkillManifest, SkillResult
from assistant.skills.base import Skill
from assistant.skills.job import WindowsJob


class IsolatedSkill(Skill):
    def __init__(
        self,
        package_dir: Path,
        manifest: SkillManifest,
        *,
        timeout_seconds: float = 30,
        memory_limit_mb: int = 256,
    ) -> None:
        self.package_dir = package_dir.resolve()
        self.manifest = manifest
        self.timeout_seconds = timeout_seconds
        self.memory_limit_mb = memory_limit_mb
        self._process: asyncio.subprocess.Process | None = None
        self._job: WindowsJob | None = None

    def input_schema(self) -> dict[str, Any]:
        return self.manifest.input_schema

    def output_schema(self) -> dict[str, Any]:
        return self.manifest.output_schema

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate(arguments, self.manifest.input_schema)
        return arguments

    def validate_result(self, result: SkillResult) -> None:
        validate(result.data, self.manifest.output_schema)

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        self.validate(call.arguments)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "assistant.skills.worker",
            str(self.package_dir),
            self.manifest.entry_point,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            env={**os.environ, "ASSISTANT_OFFLINE": "1"},
        )
        process_handle = getattr(self._process, "_transport", None)
        process_handle = getattr(getattr(process_handle, "_proc", None), "_handle", None)
        if process_handle:
            self._job = WindowsJob(
                process_handle,
                memory_mb=self.memory_limit_mb,
                cpu_seconds=max(1, int(self.timeout_seconds)),
            )
        request = json.dumps({"operation": "execute", "call": call.model_dump(mode="json")}).encode(
            "utf-8"
        )
        communicate = asyncio.create_task(self._process.communicate(request))
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {communicate, cancelled},
                timeout=self.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done or not done:
                await self._terminate()
                if cancelled in done:
                    return SkillResult(status="cancelled", summary="The skill was cancelled.")
                return SkillResult(status="timed_out", summary="The skill timed out.")
            stdout, stderr = communicate.result()
            if self._process.returncode != 0:
                raise RuntimeError(
                    stderr.decode("utf-8", errors="replace")[-2000:]
                    or "Isolated skill worker failed"
                )
            payload = json.loads(stdout.decode("utf-8"))
            if payload.get("ok") is not True:
                raise RuntimeError(payload.get("error", "Isolated skill failed"))
            result = SkillResult.model_validate(payload["result"])
            self.validate_result(result)
            return result
        finally:
            cancelled.cancel()
            if not communicate.done():
                communicate.cancel()
            await self._terminate()

    async def _terminate(self) -> None:
        if self._job:
            self._job.close()
            self._job = None
        if self._process and self._process.returncode is None:
            self._process.kill()
            await self._process.wait()
        self._process = None

    async def cancel(self, correlation_id: str) -> None:
        await self._terminate()

    async def health(self) -> dict[str, Any]:
        return {
            "healthy": self.package_dir.is_dir(),
            "isolated": True,
            "skill_id": self.manifest.skill_id,
        }
