from __future__ import annotations

import base64
from dataclasses import dataclass

from assistant.audio.capture import AudioCapture
from assistant.audio.wakeword import OpenWakeWordProvider
from assistant.config import Settings
from assistant.context.providers import (
    DesktopContextProvider,
    DeviceContextProvider,
    FileContextProvider,
    ProcessContextProvider,
)
from assistant.events import EventBus
from assistant.execution.engine import ExecutionEngine
from assistant.integrations.web import CloudModelClient, WebClient
from assistant.language.embeddings import (
    FallbackEmbeddingProvider,
    LexicalFallbackEmbeddingProvider,
    OllamaEmbeddingProvider,
)
from assistant.language.ollama import OllamaProvider
from assistant.language.planner import LocalPlanner, SkillRetriever
from assistant.language.retrieval import (
    KnowledgeSemanticIndexer,
    MemorySemanticIndexer,
    SemanticRetriever,
    SkillSemanticIndexer,
    WorkflowTemplateSemanticIndexer,
)
from assistant.logging import configure_logging
from assistant.models import ModelManager
from assistant.orchestrator import AssistantOrchestrator
from assistant.policy.confirmations import ConfirmationManager
from assistant.policy.file_access import FileAccessPolicyStore
from assistant.policy.permissions import PermissionManager
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.builtin import (
    CalculatorSkill,
    ClipboardSkill,
    DateTimeSkill,
    FileAccessPolicySkill,
    FileSearchSkill,
    HelpSkill,
    LauncherSkill,
    MediaSkill,
    RegisteredApplicationsSkill,
    TimerSkill,
)
from assistant.skills.diagnostic import DiagnosticSkill
from assistant.skills.macros import MacroSkill
from assistant.skills.memory import MemorySkill
from assistant.skills.network import CloudAskSkill, WebFetchSkill, WebSearchSkill
from assistant.skills.productivity import (
    NoteSkill,
    ReminderSkill,
    TaskSkill,
    UnitConversionSkill,
)
from assistant.skills.registry import SkillRegistry
from assistant.skills.windows import (
    ApplicationWindowSkill,
    BrowserNavigateSkill,
    ContextNavigationSkill,
    FileOperationSkill,
    InputAutomationSkill,
    NotificationSkill,
    ProcessSkill,
    WindowsDeviceControlSkill,
    WindowsSettingSkill,
)
from assistant.storage.aliases import AliasStore
from assistant.storage.database import AuditStore, Database, MetricsStore, SettingsStore
from assistant.storage.history import HistoryStore
from assistant.storage.knowledge import KnowledgeBaseStore
from assistant.storage.macros import MacroStore
from assistant.storage.memory import MemoryStore
from assistant.storage.secrets import SecretVault
from assistant.storage.vector_index import VectorIndex
from assistant.stt.faster_whisper import FasterWhisperProvider
from assistant.tts.piper import PiperProvider
from assistant.windows.adapters import (
    LocalFileSystemAdapter,
    LocalWindowsDeviceAdapter,
    LocalWindowsSettingsAdapter,
    WindowsApplicationAdapter,
    WindowsClipboardAdapter,
    WindowsInputAutomationAdapter,
    WindowsMediaAdapter,
    WindowsNotificationAdapter,
    WindowsProcessAdapter,
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
    file_access_policy: FileAccessPolicyStore
    vector_index: VectorIndex
    semantic_retriever: SemanticRetriever
    knowledge_semantic_indexer: KnowledgeSemanticIndexer
    memory_semantic_indexer: MemorySemanticIndexer
    workflow_template_indexer: WorkflowTemplateSemanticIndexer
    memory_store: MemoryStore


def _decode_trusted_keys(encoded_keys: dict[str, str]) -> dict[str, bytes]:
    return {key_id: base64.b64decode(value) for key_id, value in encoded_keys.items()}


def _settings_with_persisted_values(settings: Settings, database: Database) -> Settings:
    persisted = SettingsStore(database).get("application_settings", {})
    if not persisted:
        return settings
    return Settings.model_validate(
        {**settings.model_dump(), **persisted, "data_dir": settings.data_dir}
    )


def _register_core_skills(
    registry: SkillRegistry, file_access_policy: FileAccessPolicyStore
) -> None:
    registry.register(TimerSkill())
    registry.register(DateTimeSkill())
    registry.register(CalculatorSkill())
    registry.register(FileAccessPolicySkill(file_access_policy))


def _register_windows_skills(
    registry: SkillRegistry,
    aliases: AliasStore,
) -> None:
    filesystem = LocalFileSystemAdapter()
    applications = WindowsApplicationAdapter()

    def resolve_application(name: str) -> str | None:
        registered = aliases.get("application", name)
        if registered:
            return registered
        if name.casefold().strip() in {"browser", "default browser"}:
            return applications.default_browser_path()
        return None

    def registered_applications() -> dict[str, str]:
        values = aliases.all("application")
        default_browser = applications.default_browser_path()
        if default_browser:
            values.setdefault("browser", default_browser)
            values.setdefault("default browser", default_browser)
        return values

    registry.register(FileSearchSkill(filesystem))
    registry.register(FileOperationSkill(filesystem))
    registry.register(
        LauncherSkill(
            applications,
            resolver=resolve_application,
        )
    )
    registry.register(RegisteredApplicationsSkill(registered_applications))
    registry.register(ApplicationWindowSkill(applications))
    registry.register(ContextNavigationSkill(applications))
    registry.register(BrowserNavigateSkill(applications))
    registry.register(MediaSkill(WindowsMediaAdapter()))
    registry.register(ClipboardSkill(WindowsClipboardAdapter()))
    registry.register(NotificationSkill(WindowsNotificationAdapter()))
    registry.register(ProcessSkill(WindowsProcessAdapter()))
    registry.register(InputAutomationSkill(WindowsInputAutomationAdapter()))
    registry.register(WindowsDeviceControlSkill(LocalWindowsDeviceAdapter()))
    registry.register(WindowsSettingSkill(LocalWindowsSettingsAdapter()))


def _register_productivity_skills(
    registry: SkillRegistry, database: Database, memory_store: MemoryStore
) -> None:
    registry.register(TaskSkill(database))
    registry.register(NoteSkill(database))
    registry.register(ReminderSkill(database))
    registry.register(UnitConversionSkill())
    registry.register(MemorySkill(memory_store))


def _register_network_skills(
    registry: SkillRegistry,
    settings: Settings,
    secret_vault: SecretVault,
) -> None:
    web_client = WebClient()
    registry.register(WebSearchSkill(web_client, secret_vault.get, settings.search_provider))
    registry.register(WebFetchSkill(web_client))
    registry.register(
        CloudAskSkill(
            CloudModelClient(),
            secret_vault.get,
            {
                "openai": settings.openai_model,
                "codex": settings.openai_codex_model,
                "anthropic": settings.anthropic_model,
            },
        )
    )


@dataclass
class _StorageServices:
    audit: AuditStore
    metrics: MetricsStore
    file_access_policy: FileAccessPolicyStore
    permissions: PermissionManager
    aliases: AliasStore
    knowledge_bases: KnowledgeBaseStore
    memory_store: MemoryStore
    vector_index: VectorIndex
    secret_vault: SecretVault


@dataclass
class _RetrievalServices:
    semantic_retriever: SemanticRetriever
    skill_indexer: SkillSemanticIndexer
    knowledge_indexer: KnowledgeSemanticIndexer
    memory_indexer: MemorySemanticIndexer
    workflow_indexer: WorkflowTemplateSemanticIndexer


@dataclass
class _AudioServices:
    capture: AudioCapture
    stt: FasterWhisperProvider
    wakeword: OpenWakeWordProvider | None
    model_manager: ModelManager
    tts: PiperProvider | None


def _build_storage_services(settings: Settings, database: Database) -> _StorageServices:
    audit = AuditStore(database)
    metrics = MetricsStore(database)
    file_access_policy = FileAccessPolicyStore(SettingsStore(database))
    return _StorageServices(
        audit=audit,
        metrics=metrics,
        file_access_policy=file_access_policy,
        permissions=PermissionManager(database, audit, file_access_policy),
        aliases=AliasStore(database),
        knowledge_bases=KnowledgeBaseStore(database),
        memory_store=MemoryStore(database),
        vector_index=VectorIndex(database),
        secret_vault=SecretVault(settings.data_dir / "secrets"),
    )


def _build_retrieval_services(
    settings: Settings,
    storage: _StorageServices,
) -> _RetrievalServices:
    lexical = LexicalFallbackEmbeddingProvider()
    embedding_provider = (
        FallbackEmbeddingProvider(
            OllamaEmbeddingProvider(
                settings.embedding_model,
                timeout_seconds=settings.embedding_timeout_seconds,
            ),
            lexical,
        )
        if settings.embedding_provider == "ollama"
        else lexical
    )
    return _RetrievalServices(
        semantic_retriever=SemanticRetriever(storage.vector_index, embedding_provider),
        skill_indexer=SkillSemanticIndexer(storage.vector_index, embedding_provider),
        knowledge_indexer=KnowledgeSemanticIndexer(storage.vector_index, embedding_provider),
        memory_indexer=MemorySemanticIndexer(storage.vector_index, embedding_provider),
        workflow_indexer=WorkflowTemplateSemanticIndexer(
            storage.vector_index,
            embedding_provider,
        ),
    )


def _build_registry(
    settings: Settings,
    database: Database,
    storage: _StorageServices,
) -> tuple[SkillRegistry, MacroStore]:
    registry = SkillRegistry(
        settings.data_dir / "skills",
        trusted_signing_keys=_decode_trusted_keys(settings.trusted_skill_keys),
    )
    _register_core_skills(registry, storage.file_access_policy)
    _register_windows_skills(registry, storage.aliases)
    _register_productivity_skills(registry, database, storage.memory_store)
    _register_network_skills(registry, settings, storage.secret_vault)
    macro_store = MacroStore(database)
    registry.register(MacroSkill(macro_store, registry, storage.permissions, storage.audit))
    registry.register(HelpSkill(registry.list))
    return registry, macro_store


def _build_context_providers(
    settings: Settings,
    storage: _StorageServices,
):
    return (
        DesktopContextProvider(
            WindowsApplicationAdapter(),
            include_open_applications=settings.context_include_open_applications,
        ),
        ProcessContextProvider(
            WindowsProcessAdapter(),
            include_open_applications=settings.context_include_open_applications,
        ),
        DeviceContextProvider(
            LocalWindowsDeviceAdapter(),
            include_connected_devices=settings.context_include_connected_devices,
        ),
        FileContextProvider(
            LocalFileSystemAdapter(),
            file_policy_store=storage.file_access_policy,
            allow_file_contents=settings.context_allow_file_contents,
        ),
    )


def _build_audio_services(settings: Settings) -> _AudioServices:
    capture = AudioCapture(
        device=settings.audio_input_device,
        silence_seconds=settings.silence_timeout_seconds,
        max_seconds=settings.max_utterance_seconds,
        noise_suppression=settings.noise_suppression,
        automatic_gain_control=settings.automatic_gain_control,
        clap_threshold=settings.voice_activation_clap_threshold,
        diagnostic_dir=(
            settings.data_dir / "diagnostic-audio" if settings.diagnostic_audio else None
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
        _decode_trusted_keys(settings.trusted_model_keys),
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
            playback_state=capture.set_playback_active,
        )
    return _AudioServices(capture, stt, wakeword, model_manager, tts)


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_url)
    database.migrate()
    settings = _settings_with_persisted_values(settings, database)
    configure_logging(
        settings.data_dir,
        developer_mode=settings.profile == "development",
    )

    storage = _build_storage_services(settings, database)
    retrieval = _build_retrieval_services(settings, storage)
    registry, macro_store = _build_registry(settings, database, storage)
    context_providers = _build_context_providers(settings, storage)
    events = EventBus()
    confirmations = ConfirmationManager()
    engine = ExecutionEngine(
        registry,
        storage.permissions,
        confirmations,
        storage.audit,
        events,
        storage.metrics,
        confirmation_overrides=settings.confirmation_overrides,
    )
    llm = OllamaProvider(settings.llm_model) if settings.llm_provider == "ollama" else None
    planner = (
        LocalPlanner(
            llm,
            SkillRetriever(registry.list),
            storage.knowledge_bases,
            timeout_seconds=settings.planner_timeout_seconds,
        )
        if llm
        else None
    )
    orchestrator = AssistantOrchestrator(
        DeterministicRouter(application_names=lambda: storage.aliases.all("application")),
        engine,
        events,
        HistoryStore(database, settings.retain_transcripts_days),
        planner=planner,
        metrics=storage.metrics,
        response_detail=settings.response_detail,
        show_reasoning_steps=settings.show_reasoning_steps,
        terminal_debug=settings.terminal_debug,
        allow_sensitive_speech=settings.allow_sensitive_speech,
        hide_sensitive_display=settings.hide_sensitive_display,
        semantic_retriever=retrieval.semantic_retriever,
        skill_semantic_indexer=retrieval.skill_indexer,
        knowledge_bases=storage.knowledge_bases,
        knowledge_semantic_indexer=retrieval.knowledge_indexer,
        memory_store=storage.memory_store,
        memory_semantic_indexer=retrieval.memory_indexer,
        workflow_template_indexer=retrieval.workflow_indexer,
        context_providers=context_providers,
    )
    audio = _build_audio_services(settings)
    orchestrator.tts = audio.tts
    orchestrator.speech_mode = settings.speech_mode
    registry.register(
        DiagnosticSkill(
            registry,
            {"stt": audio.stt, "llm": llm, "tts": audio.tts},
        )
    )
    return Container(
        settings=settings,
        database=database,
        audit=storage.audit,
        permissions=storage.permissions,
        confirmations=confirmations,
        registry=registry,
        events=events,
        engine=engine,
        orchestrator=orchestrator,
        audio_capture=audio.capture,
        stt=audio.stt,
        macro_store=macro_store,
        wakeword=audio.wakeword,
        model_manager=audio.model_manager,
        metrics=storage.metrics,
        aliases=storage.aliases,
        knowledge_bases=storage.knowledge_bases,
        file_access_policy=storage.file_access_policy,
        vector_index=storage.vector_index,
        semantic_retriever=retrieval.semantic_retriever,
        knowledge_semantic_indexer=retrieval.knowledge_indexer,
        memory_semantic_indexer=retrieval.memory_indexer,
        workflow_template_indexer=retrieval.workflow_indexer,
        memory_store=storage.memory_store,
    )
