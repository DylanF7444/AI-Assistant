from __future__ import annotations

import asyncio
import json
import tempfile
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from assistant.contracts import AudioSession, Transcript
from assistant.router.normalization import normalize_text


class WhisperCppProvider:
    def __init__(self, executable: Path, model: Path) -> None:
        self.executable = executable
        self.model = model
        self._process: asyncio.subprocess.Process | None = None

    async def transcribe(
        self, session: AudioSession, audio: AsyncIterator[bytes]
    ) -> AsyncIterator[Transcript]:
        chunks = bytearray()
        async for chunk in audio:
            chunks.extend(chunk)
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "audio.wav"
            out_path = Path(directory) / "result"
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(session.channels)
                wav.setsampwidth(2)
                wav.setframerate(session.sample_rate)
                wav.writeframes(chunks)
            self._process = await asyncio.create_subprocess_exec(
                str(self.executable),
                "-m",
                str(self.model),
                "-f",
                str(wav_path),
                "-oj",
                "-of",
                str(out_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await self._process.communicate()
            if self._process.returncode:
                raise RuntimeError(stderr.decode("utf-8", errors="replace"))
            payload = json.loads((out_path.with_suffix(".json")).read_text("utf-8"))
            text = "".join(item["text"] for item in payload["transcription"]).strip()
            yield Transcript(
                session_id=session.session_id,
                raw_text=text,
                normalized_text=normalize_text(text),
                is_final=True,
            )
            self._process = None

    async def cancel(self) -> None:
        if self._process:
            self._process.terminate()

    async def health(self) -> dict[str, Any]:
        return {
            "healthy": self.executable.exists() and self.model.exists(),
            "executable": str(self.executable),
            "model": str(self.model),
        }
