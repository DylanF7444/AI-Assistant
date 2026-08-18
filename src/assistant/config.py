from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from platformdirs import user_data_path
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
LEGACY_CODEX_MODEL_DEFAULTS = frozenset({"gpt-5.3-codex"})


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
    ui_text_scale: float = Field(default=1.0, ge=1.0, le=1.75)
    ui_high_contrast: bool = False
    ui_reduced_motion: bool = False
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
    vision_model: str = ""
    vision_timeout_seconds: float = Field(default=60, ge=5, le=300)
    stt_provider: str = "faster-whisper"
    stt_model: str = "small.en"
    stt_device: Literal["auto", "cuda", "cpu"] = "auto"
    llm_provider: str = "ollama"
    llm_model: str = "qwen2.5:3b"
    llm_keep_alive: str = "30m"
    llm_preload: bool = True
    planner_timeout_seconds: float = Field(default=60.0, gt=0)
    embedding_provider: Literal["lexical", "ollama"] = "lexical"
    embedding_model: str = "nomic-embed-text"
    embedding_timeout_seconds: float = Field(default=10.0, gt=0)
    openai_model: str = "gpt-5.5"
    openai_codex_model: str = DEFAULT_CODEX_MODEL
    anthropic_model: str = "claude-sonnet-4-6"
    cloud_pricing: dict[str, dict[str, float]] = Field(default_factory=dict)
    search_provider: Literal["duckduckgo", "brave"] = "duckduckgo"
    extension_catalog_url: str | None = None
    extension_catalog_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    extension_package_max_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )
    remote_media_max_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1024,
        le=500 * 1024 * 1024,
    )
    media_cache_max_bytes: int = Field(
        default=500 * 1024 * 1024,
        ge=1024,
        le=2 * 1024 * 1024 * 1024,
    )
    media_cache_retention_days: int = Field(default=7, ge=1, le=90)
    remote_media_timeout_seconds: float = Field(default=30, gt=0, le=120)
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
    revoked_skill_key_ids: tuple[str, ...] = ()
    allow_reviewed_worker_extensions: bool = False
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
    show_in_app_notifications: bool = True
    show_system_notifications: bool = True
    notification_quiet_hours_enabled: bool = False
    notification_quiet_hours_start: str = "22:00"
    notification_quiet_hours_end: str = "07:00"

    @field_validator("cloud_pricing", mode="before")
    @classmethod
    def _cloud_pricing_is_bounded(
        cls,
        value: Any,
    ) -> Any:
        if not isinstance(value, Mapping):
            return value
        for key, prices in value.items():
            if not isinstance(key, str):
                raise ValueError("Cloud pricing keys must use provider:model format")
            if not re.fullmatch(r"[a-z][a-z0-9_-]*:[A-Za-z0-9*._-]+", key):
                raise ValueError("Cloud pricing keys must use provider:model format")
            provider = key.split(":", 1)[0]
            if provider not in {"openai", "anthropic", "codex"}:
                raise ValueError(f"Unsupported cloud pricing provider: {provider}")
            if not isinstance(prices, Mapping):
                raise ValueError(f"Cloud pricing {key} must be an object")
            unknown_fields = set(prices).difference(
                {"input_usd_per_million", "output_usd_per_million"}
            )
            if unknown_fields:
                raise ValueError(
                    f"Cloud pricing {key} contains unsupported fields: "
                    + ", ".join(sorted(unknown_fields))
                )
            for field_name in ("input_usd_per_million", "output_usd_per_million"):
                price = prices.get(field_name)
                if (
                    price is None
                    or isinstance(price, bool)
                    or not isinstance(price, int | float | str)
                    or not math.isfinite(float(price))
                    or float(price) < 0
                ):
                    raise ValueError(f"Cloud pricing {key} needs finite non-negative {field_name}")
        return value

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

    @field_validator("llm_keep_alive")
    @classmethod
    def _validate_llm_keep_alive(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"-?\d+(?:\.\d+)?(?:ms|s|m|h)?", normalized):
            raise ValueError("LLM keep-alive must be a duration such as 30m, 2h, 0, or -1")
        return normalized

    @field_validator(
        "notification_quiet_hours_start",
        "notification_quiet_hours_end",
    )
    @classmethod
    def _validate_quiet_hours_time(cls, value: str) -> str:
        normalized = value.strip()
        try:
            parsed = datetime.strptime(normalized, "%H:%M")
        except ValueError as error:
            raise ValueError("quiet-hours time must use 24-hour HH:MM format") from error
        return parsed.strftime("%H:%M")

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

    @model_validator(mode="after")
    def _media_cache_must_fit_one_download(self) -> Settings:
        if self.media_cache_max_bytes < self.remote_media_max_bytes:
            raise ValueError(
                "Media cache limit cannot be smaller than the remote media download limit"
            )
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
