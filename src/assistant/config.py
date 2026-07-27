from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from platformdirs import user_data_path
from pydantic import BaseModel, Field, field_validator, model_validator


class Settings(BaseModel):
    profile: Literal["development", "testing", "production"] = "development"
    data_dir: Path = Field(
        default_factory=lambda: user_data_path("LocalAssistant", "LocalAssistant")
    )
    database_name: str = "assistant.db"
    offline_lock: bool = True
    dashboard_history_retention_days: int = Field(default=7, ge=1, le=90)
    retain_transcripts_days: int = Field(default=0, ge=0)
    diagnostic_audio: bool = False
    voice_activation_mode: Literal["hotkey", "keyword_clap", "persistent"] = "hotkey"
    voice_activation_keyword: str = Field(default="assistant", min_length=1)
    voice_activation_keyword_enabled: bool = True
    voice_activation_clap_enabled: bool = False
    voice_activation_clap_threshold: float = Field(default=0.35, gt=0, le=1)
    speech_mode: Literal["always", "voice_only", "never"] = "voice_only"
    response_detail: Literal["short", "normal", "detailed"] = "normal"
    show_reasoning_steps: bool = False
    terminal_debug: bool = True
    allow_sensitive_speech: bool = False
    hide_sensitive_display: bool = False
    context_include_open_applications: bool = True
    context_include_connected_devices: bool = True
    context_allow_clipboard: bool = False
    context_allow_browser: bool = False
    context_allow_screen: bool = False
    context_allow_file_contents: bool = False
    stt_provider: str = "faster-whisper"
    stt_model: str = "small.en"
    stt_device: Literal["auto", "cuda", "cpu"] = "auto"
    llm_provider: str = "ollama"
    llm_model: str = "qwen2.5:3b"
    planner_timeout_seconds: float = Field(default=60.0, gt=0)
    embedding_provider: Literal["lexical", "ollama"] = "lexical"
    embedding_model: str = "nomic-embed-text"
    embedding_timeout_seconds: float = Field(default=10.0, gt=0)
    openai_model: str = "gpt-5.5"
    openai_codex_model: str = "gpt-5.3-codex"
    anthropic_model: str = "claude-sonnet-4-6"
    search_provider: Literal["duckduckgo", "brave"] = "duckduckgo"
    extension_catalog_url: str | None = None
    extension_catalog_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    extension_package_max_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )
    tts_provider: Literal["piper", "elevenlabs"] = "piper"
    piper_executable: Path | None = None
    piper_model: Path | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_model_id: str = "eleven_flash_v2_5"
    elevenlabs_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    tts_volume: float = Field(default=1.0, ge=0, le=1)
    tts_output_device: str | None = None
    noise_suppression: bool = True
    automatic_gain_control: bool = True
    wake_word_enabled: bool = False
    wake_word_model: Path | None = None
    wake_word_threshold: float = Field(default=0.5, gt=0, le=1)
    confirmation_overrides: dict[str, bool] = Field(default_factory=dict)
    trusted_model_keys: dict[str, str] = Field(default_factory=dict)
    trusted_skill_keys: dict[str, str] = Field(default_factory=dict)
    push_to_talk_hotkey: str = "ctrl+space"
    cancel_hotkey: str = "esc"
    audio_input_device: int | None = None
    audio_output_device: str | None = None
    silence_timeout_seconds: float = Field(default=1.0, gt=0)
    max_utterance_seconds: float = Field(default=30.0, gt=0)
    scheduler_enabled: bool = True
    automations_paused: bool = False
    background_actions_enabled: bool = False
    max_background_concurrency: int = Field(default=1, ge=1, le=8)
    minimum_recurrence_seconds: int = Field(default=60, ge=60)
    default_timezone: str = "UTC"
    notify_on_background_success: bool = True
    notify_on_background_failure: bool = True
    pause_background_while_locked: bool = True

    @field_validator("extension_catalog_url", mode="before")
    @classmethod
    def _extension_catalog_must_use_https(cls, value: object) -> object:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            return None
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("extension catalog URL must use HTTPS without embedded credentials")
        return normalized

    @field_validator("voice_activation_keyword")
    @classmethod
    def _keyword_must_not_be_blank(cls, value: str) -> str:
        keyword = " ".join(value.split())
        if not keyword:
            raise ValueError("voice activation keyword cannot be blank")
        return keyword

    @field_validator("elevenlabs_voice_id", mode="before")
    @classmethod
    def _normalize_elevenlabs_voice_id(cls, value: object) -> object:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            return None
        if not all(character.isalnum() or character in "_-" for character in normalized):
            raise ValueError("ElevenLabs voice ID contains unsupported characters")
        return normalized

    @field_validator("elevenlabs_model_id")
    @classmethod
    def _validate_elevenlabs_model_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or not all(
            character.islower() or character.isdigit() or character == "_"
            for character in normalized
        ):
            raise ValueError("ElevenLabs model ID must use lowercase letters, numbers, or _")
        return normalized

    @model_validator(mode="after")
    def _keyword_clap_mode_needs_trigger(self) -> Settings:
        if (
            self.voice_activation_mode == "keyword_clap"
            and not self.voice_activation_keyword_enabled
            and not self.voice_activation_clap_enabled
        ):
            raise ValueError("keyword/clap voice activation needs a keyword or clap trigger")
        return self

    @field_validator("default_timezone")
    @classmethod
    def _default_timezone_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA time zone: {value}") from exc
        return value

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_name

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"
