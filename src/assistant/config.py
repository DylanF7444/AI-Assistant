from __future__ import annotations

from pathlib import Path
from typing import Literal

from platformdirs import user_data_path
from pydantic import BaseModel, Field


class Settings(BaseModel):
    profile: Literal["development", "testing", "production"] = "development"
    data_dir: Path = Field(
        default_factory=lambda: user_data_path("LocalAssistant", "LocalAssistant")
    )
    database_name: str = "assistant.db"
    offline_lock: bool = True
    retain_transcripts_days: int = Field(default=0, ge=0)
    diagnostic_audio: bool = False
    speech_mode: Literal["always", "voice_only", "never"] = "voice_only"
    response_detail: Literal["short", "normal", "detailed"] = "normal"
    allow_sensitive_speech: bool = False
    hide_sensitive_display: bool = False
    stt_provider: str = "faster-whisper"
    stt_model: str = "small.en"
    stt_device: Literal["auto", "cuda", "cpu"] = "auto"
    llm_provider: str = "ollama"
    llm_model: str = "qwen2.5:3b"
    planner_timeout_seconds: float = Field(default=60.0, gt=0)
    openai_model: str = "gpt-5.5"
    openai_codex_model: str = "gpt-5.3-codex"
    anthropic_model: str = "claude-sonnet-4-6"
    search_provider: Literal["duckduckgo", "brave"] = "duckduckgo"
    tts_provider: str = "piper"
    piper_executable: Path | None = None
    piper_model: Path | None = None
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    tts_volume: float = Field(default=1.0, ge=0, le=1)
    tts_output_device: str | None = None
    noise_suppression: bool = True
    automatic_gain_control: bool = True
    wake_word_enabled: bool = False
    wake_word_model: Path | None = None
    confirmation_overrides: dict[str, bool] = Field(default_factory=dict)
    trusted_model_keys: dict[str, str] = Field(default_factory=dict)
    trusted_skill_keys: dict[str, str] = Field(default_factory=dict)
    push_to_talk_hotkey: str = "ctrl+space"
    cancel_hotkey: str = "esc"
    audio_input_device: int | None = None
    audio_output_device: str | None = None
    silence_timeout_seconds: float = Field(default=1.0, gt=0)
    max_utterance_seconds: float = Field(default=30.0, gt=0)

    @property
    def database_url(self) -> str:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / self.database_name).as_posix()}"
