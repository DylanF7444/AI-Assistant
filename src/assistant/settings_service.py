from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from assistant.config import Settings
from assistant.contracts import RiskLevel
from assistant.storage.database import SettingsStore

APPLICATION_SETTINGS_KEY = "application_settings"

# Keep this list explicit. Adding a Settings field must not silently expose it to
# model-selected tool calls.
ASSISTANT_SETTING_NAMES = frozenset(
    {
        "allow_sensitive_speech",
        "anthropic_model",
        "audio_input_device",
        "audio_output_device",
        "automatic_gain_control",
        "automations_paused",
        "background_actions_enabled",
        "cancel_hotkey",
        "cloud_pricing",
        "confirmation_overrides",
        "context_allow_browser",
        "context_allow_clipboard",
        "context_allow_file_contents",
        "context_allow_screen",
        "context_include_connected_devices",
        "context_include_open_applications",
        "dashboard_history_retention_days",
        "default_timezone",
        "diagnostic_audio",
        "elevenlabs_model_id",
        "elevenlabs_timeout_seconds",
        "elevenlabs_voice_id",
        "embedding_model",
        "embedding_provider",
        "embedding_timeout_seconds",
        "extension_catalog_timeout_seconds",
        "extension_catalog_url",
        "extension_package_max_bytes",
        "hide_sensitive_display",
        "llm_keep_alive",
        "llm_model",
        "llm_preload",
        "llm_provider",
        "max_background_concurrency",
        "max_utterance_seconds",
        "media_cache_max_bytes",
        "media_cache_retention_days",
        "minimum_recurrence_seconds",
        "noise_suppression",
        "notification_quiet_hours_enabled",
        "notification_quiet_hours_end",
        "notification_quiet_hours_start",
        "notify_on_background_failure",
        "notify_on_background_success",
        "offline_lock",
        "openai_codex_model",
        "openai_model",
        "pause_background_while_locked",
        "piper_executable",
        "piper_model",
        "planner_timeout_seconds",
        "push_to_talk_hotkey",
        "remote_media_max_bytes",
        "remote_media_timeout_seconds",
        "response_detail",
        "retain_transcripts_days",
        "scheduler_enabled",
        "search_provider",
        "show_in_app_notifications",
        "show_reasoning_steps",
        "show_system_notifications",
        "silence_timeout_seconds",
        "speech_mode",
        "stt_device",
        "stt_model",
        "stt_provider",
        "terminal_debug",
        "tts_output_device",
        "tts_provider",
        "tts_speed",
        "tts_volume",
        "ui_high_contrast",
        "ui_reduced_motion",
        "ui_text_scale",
        "vision_model",
        "vision_timeout_seconds",
        "voice_activation_clap_enabled",
        "voice_activation_clap_threshold",
        "voice_activation_keyword",
        "voice_activation_keyword_enabled",
        "voice_activation_mode",
        "wake_word_enabled",
        "wake_word_model",
        "wake_word_threshold",
    }
)

_CONFIRMATION_RISK = {
    "allow_sensitive_speech": RiskLevel.HIGH,
    "automations_paused": RiskLevel.HIGH,
    "background_actions_enabled": RiskLevel.HIGH,
    "confirmation_overrides": RiskLevel.HIGH,
    "context_allow_browser": RiskLevel.MEDIUM,
    "context_allow_clipboard": RiskLevel.MEDIUM,
    "context_allow_file_contents": RiskLevel.HIGH,
    "context_allow_screen": RiskLevel.HIGH,
    "diagnostic_audio": RiskLevel.HIGH,
    "extension_catalog_url": RiskLevel.MEDIUM,
    "hide_sensitive_display": RiskLevel.MEDIUM,
    "max_background_concurrency": RiskLevel.MEDIUM,
    "minimum_recurrence_seconds": RiskLevel.MEDIUM,
    "offline_lock": RiskLevel.HIGH,
    "pause_background_while_locked": RiskLevel.HIGH,
    "retain_transcripts_days": RiskLevel.MEDIUM,
    "scheduler_enabled": RiskLevel.HIGH,
    "show_reasoning_steps": RiskLevel.MEDIUM,
    "terminal_debug": RiskLevel.MEDIUM,
    "tts_provider": RiskLevel.MEDIUM,
}

_RESTART_REQUIRED = frozenset(
    {
        "audio_input_device",
        "audio_output_device",
        "embedding_model",
        "embedding_provider",
        "elevenlabs_model_id",
        "elevenlabs_voice_id",
        "llm_provider",
        "piper_executable",
        "piper_model",
        "stt_device",
        "stt_model",
        "stt_provider",
        "tts_output_device",
        "tts_provider",
        "vision_model",
        "wake_word_enabled",
        "wake_word_model",
    }
)

_SETTING_ALIASES = {
    "activation mode": "voice_activation_mode",
    "assistant response detail": "response_detail",
    "background actions": "background_actions_enabled",
    "background skill actions": "background_actions_enabled",
    "browser context": "context_allow_browser",
    "clipboard context": "context_allow_clipboard",
    "codex model": "openai_codex_model",
    "connected device context": "context_include_connected_devices",
    "connected devices context": "context_include_connected_devices",
    "default time zone": "default_timezone",
    "default timezone": "default_timezone",
    "diagnostic audio": "diagnostic_audio",
    "file content context": "context_allow_file_contents",
    "global offline lock": "offline_lock",
    "high contrast": "ui_high_contrast",
    "interface text size": "ui_text_scale",
    "local model": "llm_model",
    "model keep alive": "llm_keep_alive",
    "notification quiet hours": "notification_quiet_hours_enabled",
    "offline lock": "offline_lock",
    "offline mode": "offline_lock",
    "open application context": "context_include_open_applications",
    "reasoning steps": "show_reasoning_steps",
    "reduce motion": "ui_reduced_motion",
    "reduced motion": "ui_reduced_motion",
    "request breakdown": "show_reasoning_steps",
    "response detail": "response_detail",
    "response length": "response_detail",
    "schedule time zone": "default_timezone",
    "schedule timezone": "default_timezone",
    "scheduling": "scheduler_enabled",
    "screen context": "context_allow_screen",
    "sensitive display": "hide_sensitive_display",
    "sensitive speech": "allow_sensitive_speech",
    "speech mode": "speech_mode",
    "terminal debug": "terminal_debug",
    "text scale": "ui_text_scale",
    "transcript retention": "retain_transcripts_days",
    "transcript retention days": "retain_transcripts_days",
    "web search provider": "search_provider",
}


@dataclass(frozen=True)
class AssistantSettingDefinition:
    name: str
    label: str
    risk: RiskLevel
    restart_required: bool

    @property
    def requires_confirmation(self) -> bool:
        return self.risk != RiskLevel.LOW


@dataclass(frozen=True)
class AssistantSettingChange:
    setting: AssistantSettingDefinition
    previous_value: Any
    value: Any


def application_settings_payload(settings: Settings) -> dict[str, Any]:
    return settings.model_dump(
        mode="json",
        exclude={
            "data_dir",
            "database_name",
            "profile",
            "trusted_model_keys",
            "trusted_skill_keys",
        },
    )


def resolve_assistant_setting_name(value: str) -> str | None:
    normalized = re.sub(r"[\s_-]+", " ", value.strip().casefold())
    alias = _SETTING_ALIASES.get(normalized)
    if alias is not None:
        return alias
    candidate = normalized.replace(" ", "_")
    return candidate if candidate in ASSISTANT_SETTING_NAMES else None


def assistant_setting_definition(name: str) -> AssistantSettingDefinition:
    resolved = resolve_assistant_setting_name(name)
    if resolved is None:
        raise ValueError(f"Unsupported assistant setting: {name}")
    return AssistantSettingDefinition(
        name=resolved,
        label=resolved.replace("_", " "),
        risk=_CONFIRMATION_RISK.get(resolved, RiskLevel.LOW),
        restart_required=resolved in _RESTART_REQUIRED,
    )


class ApplicationSettingsService:
    def __init__(
        self,
        store: SettingsStore,
        current_settings: Callable[[], Settings],
        apply_settings: Callable[[Settings], None],
    ) -> None:
        self.store = store
        self.current_settings = current_settings
        self.apply_settings = apply_settings
        self._lock = threading.RLock()

    def get(self, setting: str) -> AssistantSettingChange:
        definition = assistant_setting_definition(setting)
        value = getattr(self.current_settings(), definition.name)
        return AssistantSettingChange(definition, value, value)

    def preview(self, setting: str, value: Any, *, toggle: bool = False) -> AssistantSettingChange:
        with self._lock:
            current = self.current_settings().model_copy(deep=True)
            definition = assistant_setting_definition(setting)
            previous = getattr(current, definition.name)
            requested = not previous if toggle and isinstance(previous, bool) else value
            if toggle and not isinstance(previous, bool):
                raise ValueError(f"{definition.label} is not an on/off setting")
            updated = self._validated_update(current, definition.name, requested)
            return AssistantSettingChange(
                definition,
                previous,
                getattr(updated, definition.name),
            )

    def update(self, setting: str, value: Any, *, toggle: bool = False) -> AssistantSettingChange:
        with self._lock:
            current = self.current_settings().model_copy(deep=True)
            change = self.preview(setting, value, toggle=toggle)
            updated = self._validated_update(current, change.setting.name, change.value)
            previous_persisted = self.store.get(APPLICATION_SETTINGS_KEY, None)
            self.store.set(APPLICATION_SETTINGS_KEY, application_settings_payload(updated))
            try:
                self.apply_settings(updated)
            except Exception:
                self.store.set(
                    APPLICATION_SETTINGS_KEY,
                    (
                        previous_persisted
                        if previous_persisted is not None
                        else application_settings_payload(current)
                    ),
                )
                self.apply_settings(current)
                raise
            return change

    @staticmethod
    def _validated_update(current: Settings, setting: str, value: Any) -> Settings:
        payload = current.model_dump()
        payload[setting] = value
        try:
            return Settings.model_validate(payload)
        except ValidationError as first_error:
            if not isinstance(value, str):
                raise ValueError(_validation_message(first_error)) from first_error
            normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
            if normalized == value:
                raise ValueError(_validation_message(first_error)) from first_error
            payload[setting] = normalized
            try:
                return Settings.model_validate(payload)
            except ValidationError as second_error:
                raise ValueError(_validation_message(second_error)) from second_error


def parse_assistant_setting_value(value: str) -> Any:
    cleaned = value.strip()
    lowered = cleaned.casefold()
    if lowered in {"on", "enabled", "enable", "true", "yes"}:
        return True
    if lowered in {"off", "disabled", "disable", "false", "no"}:
        return False
    if lowered in {"none", "null", "not set", "default"}:
        return None
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return cleaned


def _validation_message(error: ValidationError) -> str:
    first = error.errors(include_url=False)[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = first.get("msg", "Invalid setting value")
    return f"{location}: {message}" if location else str(message)
