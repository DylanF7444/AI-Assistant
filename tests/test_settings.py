from assistant.bootstrap import build_container
from assistant.storage.aliases import AliasStore
from assistant.storage.database import SettingsStore


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
    assert second.settings.context_include_open_applications is False
    assert second.settings.context_include_connected_devices is False
    assert second.settings.context_allow_clipboard is True
    assert second.settings.context_allow_browser is True
    assert second.settings.context_allow_screen is True
    assert second.settings.context_allow_file_contents is True
    assert second.settings.voice_activation_mode == "keyword_clap"
    assert second.settings.voice_activation_keyword == "computer"
    assert second.settings.voice_activation_clap_enabled is True


def test_duckduckgo_is_the_default_search_provider(settings):
    container = build_container(settings)
    assert container.settings.search_provider == "duckduckgo"


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
