from datetime import datetime

import pytest

import assistant.bootstrap as bootstrap
from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.storage.aliases import AliasStore
from assistant.storage.database import SettingsStore
from assistant.tts.elevenlabs import ElevenLabsProvider
from assistant.ui.desktop.app import _accessible_stylesheet, _notification_quiet_hours_active


def test_database_path_and_url_are_pure(settings, tmp_path):
    data_dir = tmp_path / "not-created"
    isolated = settings.model_copy(update={"data_dir": data_dir})

    assert isolated.database_path == data_dir / isolated.database_name
    assert isolated.database_url.endswith("/not-created/assistant.db")
    assert not data_dir.exists()


def test_persisted_settings_load_on_restart(settings):
    first = build_container(settings)
    SettingsStore(first.database).set(
        "application_settings",
        {
            "speech_mode": "never",
            "stt_model": "base.en",
            "retain_transcripts_days": 7,
            "show_reasoning_steps": True,
            "terminal_debug": False,
            "llm_keep_alive": "2h",
            "llm_preload": False,
            "context_include_open_applications": False,
            "context_include_connected_devices": False,
            "context_allow_clipboard": True,
            "context_allow_browser": True,
            "context_allow_screen": True,
            "context_allow_file_contents": True,
            "voice_activation_mode": "keyword_clap",
            "voice_activation_keyword": "computer",
            "voice_activation_clap_enabled": True,
        },
    )
    second = build_container(settings)
    assert second.settings.speech_mode == "never"
    assert second.settings.stt_model == "base.en"
    assert second.settings.retain_transcripts_days == 7
    assert second.settings.show_reasoning_steps is True
    assert second.settings.terminal_debug is False
    assert second.settings.llm_keep_alive == "2h"
    assert second.settings.llm_preload is False
    assert second.settings.context_include_open_applications is False
    assert second.settings.context_include_connected_devices is False
    assert second.settings.context_allow_clipboard is True
    assert second.settings.context_allow_browser is True
    assert second.settings.context_allow_screen is True
    assert second.settings.context_allow_file_contents is True
    assert second.settings.voice_activation_mode == "keyword_clap"
    assert second.settings.voice_activation_keyword == "computer"
    assert second.settings.voice_activation_clap_enabled is True


def test_ollama_keep_alive_validates_supported_duration_syntax(tmp_path):
    configured = Settings(
        profile="testing",
        data_dir=tmp_path,
        llm_keep_alive=" 2H ",
    )

    assert configured.llm_keep_alive == "2h"
    with pytest.raises(ValueError, match="keep-alive"):
        Settings(
            profile="testing",
            data_dir=tmp_path,
            llm_keep_alive="forever",
        )


def test_duckduckgo_is_the_default_search_provider(settings):
    container = build_container(settings)
    assert container.settings.search_provider == "duckduckgo"


def test_accessibility_settings_are_bounded_persisted_and_transform_styles(settings):
    first = build_container(settings)
    SettingsStore(first.database).set(
        "application_settings",
        {"ui_text_scale": 1.5, "ui_high_contrast": True, "ui_reduced_motion": True},
    )
    restored = build_container(settings).settings
    rendered = _accessible_stylesheet(
        "QLabel { font-size: 10pt; color: #dce7f3; }",
        text_scale=restored.ui_text_scale,
        high_contrast=restored.ui_high_contrast,
    )

    assert restored.ui_reduced_motion is True
    assert "font-size: 15pt" in rendered
    assert "border: 3px solid #00ffff" in rendered
    with pytest.raises(ValueError, match="less than or equal to 1.75"):
        Settings(data_dir=settings.data_dir, ui_text_scale=2.0)


def test_notification_quiet_hours_validate_and_handle_overnight_window(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        notification_quiet_hours_enabled=True,
        notification_quiet_hours_start="22:00",
        notification_quiet_hours_end="07:00",
    )

    assert _notification_quiet_hours_active(settings, datetime(2026, 7, 28, 23, 30))
    assert _notification_quiet_hours_active(settings, datetime(2026, 7, 28, 6, 30))
    assert not _notification_quiet_hours_active(settings, datetime(2026, 7, 28, 12, 0))

    with pytest.raises(ValueError, match="HH:MM"):
        Settings(
            profile="testing",
            data_dir=tmp_path,
            notification_quiet_hours_start="night",
        )


def test_alias_store_batch_registers_applications(settings):
    container = build_container(settings)
    store = AliasStore(container.database)

    added, updated = store.set_many(
        "application",
        {
            "Notepad": r"C:\Windows\notepad.exe",
            "Calculator": r"C:\Windows\calc.exe",
        },
    )

    assert (added, updated) == (2, 0)
    assert store.get("application", "notepad") == r"C:\Windows\notepad.exe"

    added, updated = store.set_many(
        "application",
        {
            "Notepad": r"C:\Windows\System32\notepad.exe",
            "Calculator": r"C:\Windows\calc.exe",
        },
    )

    assert (added, updated) == (0, 1)
    assert store.get("application", "notepad") == r"C:\Windows\System32\notepad.exe"


def test_wake_word_provider_uses_selected_microphone_and_threshold(settings, tmp_path, monkeypatch):
    import assistant.bootstrap as bootstrap

    model = tmp_path / "wake.onnx"
    model.write_bytes(b"model")
    captured = {}

    class FakeWakeWordProvider:
        def __init__(self, model_path, *, threshold, device):
            captured.update(model_path=model_path, threshold=threshold, device=device)

    monkeypatch.setattr(bootstrap, "OpenWakeWordProvider", FakeWakeWordProvider)
    configured = settings.model_copy(
        update={
            "wake_word_enabled": True,
            "wake_word_model": model,
            "wake_word_threshold": 0.72,
            "audio_input_device": 4,
        }
    )

    services = bootstrap._build_audio_services(configured)

    assert services.wakeword is not None
    assert captured == {"model_path": model, "threshold": 0.72, "device": 4}


def test_audio_services_build_selected_elevenlabs_provider(settings):
    class FakeVault:
        def get(self, name):
            assert name == "elevenlabs"
            return "secret-key"

    configured = settings.model_copy(
        update={
            "tts_provider": "elevenlabs",
            "elevenlabs_voice_id": "voice_123",
            "elevenlabs_model_id": "eleven_multilingual_v2",
            "offline_lock": True,
        }
    )

    services = bootstrap._build_audio_services(configured, FakeVault())

    assert isinstance(services.tts, ElevenLabsProvider)
    assert services.tts.voice_id == "voice_123"
    assert services.tts.model_id == "eleven_multilingual_v2"
    assert services.tts.offline_lock is True
