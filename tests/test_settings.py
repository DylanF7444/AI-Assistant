from assistant.bootstrap import build_container
from assistant.storage.database import SettingsStore


def test_persisted_settings_load_on_restart(settings):
    first = build_container(settings)
    SettingsStore(first.database).set(
        "application_settings",
        {
            "speech_mode": "never",
            "stt_model": "base.en",
            "retain_transcripts_days": 7,
        },
    )
    second = build_container(settings)
    assert second.settings.speech_mode == "never"
    assert second.settings.stt_model == "base.en"
    assert second.settings.retain_transcripts_days == 7


def test_duckduckgo_is_the_default_search_provider(settings):
    container = build_container(settings)
    assert container.settings.search_provider == "duckduckgo"
