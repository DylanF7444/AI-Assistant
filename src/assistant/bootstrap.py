from __future__ import annotations

import base64
from dataclasses import dataclass

from assistant.config import Settings
from assistant.audio.capture import AudioCapture
from assistant.audio.wakeword import OpenWakeWordProvider
from assistant.events import EventBus
from assistant.execution.engine import ExecutionEngine
from assistant.orchestrator import AssistantOrchestrator
from assistant.policy.confirmations import ConfirmationManager
from assistant.policy.permissions import PermissionManager
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.builtin import (
    CalculatorSkill,
    ClipboardSkill,
    DateTimeSkill,
    FileSearchSkill,
    HelpSkill,
    LauncherSkill,
    MediaSkill,
    TimerSkill,
)
from assistant.skills.registry import SkillRegistry
from assistant.skills.productivity import (
    NoteSkill,
    ReminderSkill,
    TaskSkill,
    UnitConversionSkill,
)
from assistant.skills.windows import (
    ApplicationWindowSkill,
    FileOperationSkill,
    NotificationSkill,
    ProcessSkill,
    InputAutomationSkill,
    WindowsSettingSkill,
)
from assistant.skills.macros import MacroSkill
from assistant.skills.diagnostic import DiagnosticSkill
from assistant.storage.database import AuditStore, Database, SettingsStore
from assistant.storage.database import MetricsStore
from assistant.logging import configure_logging
from assistant.storage.history import HistoryStore
from assistant.storage.macros import MacroStore
from assistant.storage.aliases import AliasStore
from assistant.storage.knowledge import KnowledgeBaseStore
from assistant.stt.faster_whisper import FasterWhisperProvider
from assistant.tts.piper import PiperProvider
from assistant.models import ModelManager
from assistant.language.ollama import OllamaProvider
from assistant.language.planner import LocalPlanner, SkillRetriever
from assistant.windows.adapters import (
    LocalFileSystemAdapter,
    WindowsApplicationAdapter,
    WindowsClipboardAdapter,
    WindowsMediaAdapter,
    WindowsNotificationAdapter,
    WindowsProcessAdapter,
    WindowsInputAutomationAdapter,
    LocalWindowsSettingsAdapter,
)


@dataclass
class Container:
    settings: Settings
    database: Database
    audit: AuditStore
    permissions: PermissionManager
    confirmations: ConfirmationManager
    registry: SkillRegistry
    events: EventBus
    engine: ExecutionEngine
    orchestrator: AssistantOrchestrator
    audio_capture: AudioCapture
    stt: FasterWhisperProvider
    macro_store: MacroStore
    wakeword: OpenWakeWordProvider | None
    model_manager: ModelManager
    metrics: MetricsStore
    aliases: AliasStore
    knowledge_bases: KnowledgeBaseStore


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    database = Database(settings.database_url)
    database.migrate()
    persisted = SettingsStore(database).get("application_settings", {})
    if persisted:
        settings = Settings.model_validate(
            {**settings.model_dump(), **persisted, "data_dir": settings.data_dir}
        )
    configure_logging(
        settings.data_dir,
        developer_mode=settings.profile == "development",
    )
    audit = AuditStore(database)
    metrics = MetricsStore(database)
    permissions = PermissionManager(database, audit)
    aliases = AliasStore(database)
    knowledge_bases = KnowledgeBaseStore(database)
    registry = SkillRegistry(
        settings.data_dir / "skills",
        trusted_signing_keys={
            key_id: base64.b64decode(value)
            for key_id, value in settings.trusted_skill_keys.items()
        },
    )
    registry.register(TimerSkill())
    registry.register(DateTimeSkill())
    registry.register(CalculatorSkill())
    filesystem = LocalFileSystemAdapter()
    applications = WindowsApplicationAdapter()
    registry.register(FileSearchSkill(filesystem))
    registry.register(FileOperationSkill(filesystem))
    registry.register(
        LauncherSkill(
            applications,
            resolver=lambda name: aliases.get("application", name),
        )
    )
    registry.register(ApplicationWindowSkill(applications))
    registry.register(MediaSkill(WindowsMediaAdapter()))
    registry.register(ClipboardSkill(WindowsClipboardAdapter()))
    registry.register(NotificationSkill(WindowsNotificationAdapter()))
    registry.register(ProcessSkill(WindowsProcessAdapter()))
    registry.register(InputAutomationSkill(WindowsInputAutomationAdapter()))
    registry.register(WindowsSettingSkill(LocalWindowsSettingsAdapter()))
    registry.register(TaskSkill(database))
    registry.register(NoteSkill(database))
    registry.register(ReminderSkill(database))
    registry.register(UnitConversionSkill())
    macro_store = MacroStore(database)
    registry.register(MacroSkill(macro_store, registry, permissions, audit))
    registry.register(HelpSkill(registry.list))
    events = EventBus()
    confirmations = ConfirmationManager()
    engine = ExecutionEngine(
        registry, permissions, confirmations, audit, events, metrics
    )
    history = HistoryStore(database, settings.retain_transcripts_days)
    llm = (
        OllamaProvider(settings.llm_model)
        if settings.llm_provider == "ollama"
        else None
    )
    planner = (
        LocalPlanner(llm, SkillRetriever(registry.list), knowledge_bases)
        if llm
        else None
    )
    orchestrator = AssistantOrchestrator(
        DeterministicRouter(),
        engine,
        events,
        history,
        planner=planner,
        metrics=metrics,
        response_detail=settings.response_detail,
        allow_sensitive_speech=settings.allow_sensitive_speech,
        hide_sensitive_display=settings.hide_sensitive_display,
    )
    audio_capture = AudioCapture(
        device=settings.audio_input_device,
        silence_seconds=settings.silence_timeout_seconds,
        max_seconds=settings.max_utterance_seconds,
        noise_suppression=settings.noise_suppression,
        automatic_gain_control=settings.automatic_gain_control,
        diagnostic_dir=(
            settings.data_dir / "diagnostic-audio"
            if settings.diagnostic_audio
            else None
        ),
    )
    stt = FasterWhisperProvider(settings.stt_model, settings.stt_device)
    wakeword = (
        OpenWakeWordProvider(settings.wake_word_model)
        if settings.wake_word_enabled
        and settings.wake_word_model
        and settings.wake_word_model.is_file()
        else None
    )
    model_manager = ModelManager(
        settings.data_dir / "models",
        {
            key_id: base64.b64decode(value)
            for key_id, value in settings.trusted_model_keys.items()
        },
    )
    tts = None
    if (
        settings.piper_executable
        and settings.piper_model
        and settings.piper_executable.is_file()
        and settings.piper_model.is_file()
    ):
        tts = PiperProvider(
            settings.piper_executable,
            settings.piper_model,
            settings.tts_output_device,
            speed=settings.tts_speed,
            volume=settings.tts_volume,
            cache_dir=settings.data_dir / "tts-cache",
            playback_state=audio_capture.set_playback_active,
        )
    orchestrator.tts = tts
    orchestrator.speech_mode = settings.speech_mode
    registry.register(
        DiagnosticSkill(
            registry, {"stt": stt, "llm": llm, "tts": tts}
        )
    )
    return Container(
        settings=settings,
        database=database,
        audit=audit,
        permissions=permissions,
        confirmations=confirmations,
        registry=registry,
        events=events,
        engine=engine,
        orchestrator=orchestrator,
        audio_capture=audio_capture,
        stt=stt,
        macro_store=macro_store,
        wakeword=wakeword,
        model_manager=model_manager,
        metrics=metrics,
        aliases=aliases,
        knowledge_bases=knowledge_bases,
    )
