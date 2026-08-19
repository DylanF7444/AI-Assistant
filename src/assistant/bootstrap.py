from __future__ import annotations

import base64
import logging
import re
from contextlib import suppress
from dataclasses import dataclass

from assistant.audio.capture import AudioCapture
from assistant.audio.focus import AudioFocusCoordinator
from assistant.audio.wakeword import OpenWakeWordProvider
from assistant.command_center.artifacts import CommandCenterArtifactStore
from assistant.command_center.builtins import register_builtin_command_center_contributions
from assistant.command_center.observations import CommandCenterObservationStore
from assistant.command_center.registry import CommandCenterRegistry
from assistant.command_center.service import CommandCenterService
from assistant.command_center.storage import CommandCenterBindingStore
from assistant.config import DEFAULT_CODEX_MODEL, LEGACY_CODEX_MODEL_DEFAULTS, Settings
from assistant.context.providers import (
    DesktopContextProvider,
    DeviceContextProvider,
    FileContextProvider,
    ProcessContextProvider,
)
from assistant.dashboard.builtins import (
    AssistantMetricsProvider,
    AutomationMetricsProvider,
    TaskMetricsProvider,
    default_dashboard_layout,
)
from assistant.dashboard.device import WindowsDeviceMetricProvider
from assistant.dashboard.home_assistant import HomeAssistantMetricProvider
from assistant.dashboard.registry import DashboardProviderRegistry
from assistant.dashboard.service import DashboardService
from assistant.dashboard.storage import (
    DashboardConnectionStore,
    DashboardLayoutStore,
    DashboardSampleStore,
)
from assistant.events import EventBus
from assistant.execution.engine import ExecutionEngine
from assistant.extensions.platform import (
    ExtensionHubService,
    ExtensionPlatformClient,
    ManagedExtensionRegistry,
    ManagedExtensionSpec,
)
from assistant.integrations.anthropic import (
    ANTHROPIC_API_KEY_SECRET,
    ANTHROPIC_LEGACY_SECRET_NAMES,
    AnthropicConnectionService,
)
from assistant.integrations.api_token import ApiTokenProvider
from assistant.integrations.cloud import CloudPricing
from assistant.integrations.connections import ConnectionBinding
from assistant.integrations.github import GitHubAdapter, GitHubConnectionService
from assistant.integrations.gmail import (
    GMAIL_SCOPE,
    GmailAdapter,
)
from assistant.integrations.google import GoogleConnectionService, GoogleTokenManager
from assistant.integrations.google_calendar import (
    CALENDAR_SCOPE,
    GoogleCalendarAdapter,
)
from assistant.integrations.google_drive import (
    DRIVE_FILE_SCOPE,
    DRIVE_READ_SCOPE,
    GoogleDriveAdapter,
)
from assistant.integrations.headless_browser import PlaywrightHeadlessBrowser
from assistant.integrations.microsoft_graph import (
    ONEDRIVE_READ_SCOPE,
    ONEDRIVE_WRITE_SCOPE,
    OUTLOOK_CALENDAR_READ_SCOPE,
    OUTLOOK_CALENDAR_WRITE_SCOPE,
    OUTLOOK_MAIL_READ_SCOPE,
    OUTLOOK_MAIL_SEND_SCOPE,
    OUTLOOK_MAIL_WRITE_SCOPE,
    MicrosoftGraphAdapter,
    MicrosoftGraphConnectionService,
    MicrosoftGraphTokenManager,
)
from assistant.integrations.notion import NotionAdapter, NotionConnectionService
from assistant.integrations.openai import (
    OPENAI_API_KEY_SECRET,
    OPENAI_LEGACY_SECRET_NAMES,
    OpenAIConnectionService,
)
from assistant.integrations.slack import SlackAdapter, SlackConnectionService
from assistant.integrations.web import WebClient
from assistant.language.embeddings import (
    FallbackEmbeddingProvider,
    LexicalFallbackEmbeddingProvider,
    OllamaEmbeddingProvider,
)
from assistant.language.ollama import OllamaProvider, OllamaUsage
from assistant.language.planner import LocalPlanner, SkillRetriever
from assistant.language.retrieval import (
    KnowledgeSemanticIndexer,
    MemorySemanticIndexer,
    SemanticRetriever,
    SkillSemanticIndexer,
    WorkflowTemplateSemanticIndexer,
)
from assistant.logging import configure_logging
from assistant.logic.store import LogicStateStore
from assistant.models import ModelManager
from assistant.observability import MetricDimensions, RequestPath
from assistant.orchestrator import AssistantOrchestrator
from assistant.policy.confirmations import ConfirmationManager
from assistant.policy.file_access import FileAccessPolicyStore
from assistant.policy.permissions import PermissionManager
from assistant.presentation.media_resolver import MediaResolver
from assistant.presentation.remote_media import RemoteMediaCache
from assistant.presentation.session import MediaSessionRegistry
from assistant.providers import EmbeddingProvider, TTSProvider
from assistant.router.deterministic import DeterministicRouter
from assistant.scheduling.authoring import ScheduleAuthoringService
from assistant.scheduling.event_sources import (
    CommandCenterEventSource,
    DeviceEventSource,
    FileEventSource,
    ProcessEventSource,
    WebEventSource,
)
from assistant.scheduling.notifications import NotificationCenter, NotificationStore
from assistant.scheduling.runner import BackgroundRunner
from assistant.scheduling.runtime import BackgroundRuntime
from assistant.scheduling.service import SchedulerService
from assistant.scheduling.store import ScheduleStore
from assistant.settings_service import ApplicationSettingsService
from assistant.skills.anthropic import AnthropicSkill
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
from assistant.skills.documents import DocumentContentSearchSkill, LocalDocumentSkill
from assistant.skills.github import GitHubSkill
from assistant.skills.gmail import GmailSkill
from assistant.skills.google_calendar import GoogleCalendarSkill
from assistant.skills.google_drive import GoogleDriveSkill
from assistant.skills.logic import ChanceSkill, ChooseSkill
from assistant.skills.macros import MacroSkill
from assistant.skills.media import MediaPresentSkill, MediaViewerControlSkill
from assistant.skills.memory import MemorySkill
from assistant.skills.network import (
    CodexAskSkill,
    HeadlessReadSkill,
    NewsSearchSkill,
    WebFetchSkill,
    WebSearchSkill,
)
from assistant.skills.notion import NotionSkill
from assistant.skills.onedrive import OneDriveSkill
from assistant.skills.openai import OpenAISkill
from assistant.skills.outlook_calendar import OutlookCalendarSkill
from assistant.skills.outlook_mail import OutlookMailSkill
from assistant.skills.playbooks import PlaybookSkill
from assistant.skills.productivity import (
    NoteSkill,
    ReminderSkill,
    TaskSkill,
    UnitConversionSkill,
)
from assistant.skills.registry import SkillRegistry
from assistant.skills.scheduling import ScheduleSkill
from assistant.skills.settings import AssistantSettingSkill
from assistant.skills.slack import SlackSkill
from assistant.skills.vision import OllamaVisionProvider, VisualUnderstandingSkill
from assistant.skills.visualize import VisualizeSkill
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
from assistant.skills.work_brief import WorkBriefSkill
from assistant.storage.aliases import AliasStore
from assistant.storage.connections import ConnectionProfileStore
from assistant.storage.database import AuditStore, Database, MetricsStore, SettingsStore
from assistant.storage.history import HistoryStore
from assistant.storage.knowledge import KnowledgeBaseStore
from assistant.storage.macros import MacroStore
from assistant.storage.memory import MemoryStore
from assistant.storage.playbooks import PlaybookStore
from assistant.storage.secrets import SecretVault
from assistant.storage.vector_index import VectorIndex
from assistant.stt.faster_whisper import FasterWhisperProvider
from assistant.tts.elevenlabs import ElevenLabsProvider
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
    extension_hub: ExtensionHubService
    events: EventBus
    engine: ExecutionEngine
    orchestrator: AssistantOrchestrator
    audio_capture: AudioCapture
    audio_focus: AudioFocusCoordinator
    stt: FasterWhisperProvider
    macro_store: MacroStore
    wakeword: OpenWakeWordProvider | None
    model_manager: ModelManager
    metrics: MetricsStore
    aliases: AliasStore
    knowledge_bases: KnowledgeBaseStore
    playbook_store: PlaybookStore
    file_access_policy: FileAccessPolicyStore
    vector_index: VectorIndex
    semantic_retriever: SemanticRetriever
    knowledge_semantic_indexer: KnowledgeSemanticIndexer
    memory_semantic_indexer: MemorySemanticIndexer
    workflow_template_indexer: WorkflowTemplateSemanticIndexer
    memory_store: MemoryStore
    logic_state: LogicStateStore
    schedule_store: ScheduleStore
    notification_store: NotificationStore
    notification_center: NotificationCenter
    scheduler: SchedulerService
    background_runtime: BackgroundRuntime
    dashboard: DashboardService
    command_center: CommandCenterService
    command_center_authoring: ScheduleAuthoringService
    secret_vault: SecretVault
    connection_profiles: ConnectionProfileStore
    media_sessions: MediaSessionRegistry
    media_cache: RemoteMediaCache


def _apply_application_settings(container: Container, updated: Settings) -> None:
    current = container.settings
    scheduler_was_enabled = current.scheduler_enabled
    for field_name in Settings.model_fields:
        setattr(current, field_name, getattr(updated, field_name))

    pricing = CloudPricing.from_mapping(current.cloud_pricing) if current.cloud_pricing else None
    container.dashboard.set_offline_lock(current.offline_lock)
    container.dashboard.set_history_retention_days(current.dashboard_history_retention_days)
    container.extension_hub.configure(
        current.extension_catalog_url,
        timeout_seconds=current.extension_catalog_timeout_seconds,
    )
    container.extension_hub.set_offline_lock(current.offline_lock)
    container.extension_hub.set_cloud_pricing(pricing)
    container.media_cache.set_offline_lock(current.offline_lock)

    container.scheduler.enabled = current.scheduler_enabled
    container.scheduler.runner.background_actions_enabled = current.background_actions_enabled
    container.scheduler.runner.pause_while_locked = current.pause_background_while_locked
    container.scheduler.runner.notify_on_success = current.notify_on_background_success
    container.scheduler.runner.notify_on_failure = current.notify_on_background_failure
    container.scheduler.max_concurrency = current.max_background_concurrency
    if (
        current.scheduler_enabled
        and not scheduler_was_enabled
        and not container.background_runtime.running
    ):
        container.background_runtime.start()
    if current.automations_paused:
        container.scheduler.pause()
    else:
        container.scheduler.resume()
    container.scheduler.wake()

    container.orchestrator.speech_mode = current.speech_mode
    container.orchestrator.response_detail = current.response_detail
    container.orchestrator.show_reasoning_steps = current.show_reasoning_steps
    container.orchestrator.terminal_debug = current.terminal_debug
    container.orchestrator.allow_sensitive_speech = current.allow_sensitive_speech
    container.orchestrator.hide_sensitive_display = current.hide_sensitive_display
    container.orchestrator.history.retention_days = current.retain_transcripts_days
    container.engine.confirmation_overrides = dict(current.confirmation_overrides)

    planner = container.orchestrator.planner
    if planner is not None:
        planner.response_detail = current.response_detail
        provider = getattr(planner, "provider", None)
        if provider is not None:
            if hasattr(provider, "model"):
                provider.model = current.llm_model
            if hasattr(provider, "keep_alive"):
                provider.keep_alive = current.llm_keep_alive

    for provider in container.orchestrator.context_providers:
        if hasattr(provider, "include_open_applications"):
            provider.include_open_applications = current.context_include_open_applications
        if hasattr(provider, "include_connected_devices"):
            provider.include_connected_devices = current.context_include_connected_devices
        if hasattr(provider, "allow_file_contents"):
            provider.allow_file_contents = current.context_allow_file_contents

    container.audio_capture.noise_suppression = current.noise_suppression
    container.audio_capture.automatic_gain_control = current.automatic_gain_control
    container.audio_capture.clap_detector.threshold = current.voice_activation_clap_threshold
    tts = container.orchestrator.tts
    if tts is not None:
        if hasattr(tts, "speed"):
            tts.speed = current.tts_speed
        if hasattr(tts, "volume"):
            tts.volume = current.tts_volume
        setter = getattr(tts, "set_offline_lock", None)
        if callable(setter):
            setter(current.offline_lock or current.tts_provider != "elevenlabs")

    with suppress(KeyError):
        container.registry.inspect("web.search").provider = current.search_provider

    model_defaults = {
        "cloud.ask": {"default_model": current.openai_codex_model},
        "integration.openai": {
            "default_model": current.openai_model,
            "default_codex_model": current.openai_codex_model,
            "default_anthropic_model": current.anthropic_model,
        },
        "integration.anthropic": {
            "default_model": current.anthropic_model,
            "default_openai_model": current.openai_model,
            "default_codex_model": current.openai_codex_model,
        },
    }
    for skill_id, defaults in model_defaults.items():
        try:
            skill = container.registry.inspect(skill_id)
        except KeyError:
            continue
        for attribute, value in defaults.items():
            if hasattr(skill, attribute):
                setattr(skill, attribute, value)


def _decode_trusted_keys(encoded_keys: dict[str, str]) -> dict[str, bytes]:
    return {key_id: base64.b64decode(value) for key_id, value in encoded_keys.items()}


def _settings_with_persisted_values(settings: Settings, database: Database) -> Settings:
    persisted = SettingsStore(database).get("application_settings", {})
    if not persisted:
        return settings
    persisted = dict(persisted)
    if persisted.get("openai_codex_model") in LEGACY_CODEX_MODEL_DEFAULTS:
        persisted["openai_codex_model"] = DEFAULT_CODEX_MODEL
    return Settings.model_validate(
        {**settings.model_dump(), **persisted, "data_dir": settings.data_dir}
    )


def _record_ollama_usage(metrics: MetricsStore, usage: OllamaUsage) -> None:
    correlation_id = usage.correlation_id or "startup"
    metrics.record(
        correlation_id=correlation_id,
        stage="model.preload" if usage.operation == "preload" else "model.total",
        duration_ms=usage.total_duration_ms or usage.wall_duration_ms,
        outcome=usage.operation,
        dimensions=MetricDimensions(path=RequestPath.PLANNER, provider="OllamaProvider"),
    )
    metrics.record(
        correlation_id=correlation_id,
        stage="model.load",
        duration_ms=usage.load_duration_ms,
        outcome=usage.operation,
        dimensions=MetricDimensions(path=RequestPath.PLANNER, provider="OllamaProvider"),
    )
    metrics.record(
        correlation_id=correlation_id,
        stage="model.prompt_eval",
        duration_ms=usage.prompt_eval_duration_ms,
        value=usage.prompt_tokens,
        outcome=usage.operation,
        dimensions=MetricDimensions(path=RequestPath.PLANNER, provider="OllamaProvider"),
    )
    metrics.record(
        correlation_id=correlation_id,
        stage="model.generation",
        duration_ms=usage.generation_duration_ms,
        value=usage.output_tokens,
        outcome=usage.operation,
        dimensions=MetricDimensions(path=RequestPath.PLANNER, provider="OllamaProvider"),
    )


def _is_browser_reference(name: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:(?:my|the)\s+)?(?:(?:default|preferred|usual|regular)\s+)?browser",
            name.strip(),
            flags=re.IGNORECASE,
        )
    )


def _preferred_registered_browser(
    aliases: AliasStore,
    memory_store: MemoryStore,
) -> str | None:
    """Resolve a browser preference only to an already registered application.

    Memories can choose among approved application aliases, but cannot introduce
    or execute an unregistered path.
    """
    registered = aliases.all("application")
    if not registered:
        return None
    browser_memories = [
        item
        for item in memory_store.list(type="preference")
        if "browser" in item.key.casefold()
        or re.search(r"\b(?:browser|browsing)\b", item.value, flags=re.IGNORECASE)
    ]
    for item in sorted(browser_memories, key=lambda value: (-value.confidence, value.id)):
        for alias in sorted(registered, key=len, reverse=True):
            if re.search(
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
                item.value,
                flags=re.IGNORECASE,
            ):
                return registered[alias]
        memory_terms = set(re.findall(r"[a-z0-9]+", item.value.casefold()))
        matching_paths = {
            path
            for alias, path in registered.items()
            if (
                alias_terms := set(re.findall(r"[a-z0-9]+", alias.casefold()))
                - {"app", "application", "browser"}
            )
            and alias_terms <= memory_terms
        }
        if len(matching_paths) == 1:
            return matching_paths.pop()
    return None


def _register_core_skills(
    registry: SkillRegistry,
    file_access_policy: FileAccessPolicyStore,
    logic_state: LogicStateStore,
) -> None:
    registry.register(TimerSkill())
    registry.register(DateTimeSkill())
    registry.register(CalculatorSkill())
    registry.register(VisualizeSkill())
    registry.register(ChooseSkill(logic_state))
    registry.register(FileAccessPolicySkill(file_access_policy))


def _register_windows_skills(
    registry: SkillRegistry,
    aliases: AliasStore,
    memory_store: MemoryStore,
    logic_state: LogicStateStore,
) -> None:
    filesystem = LocalFileSystemAdapter()
    applications = WindowsApplicationAdapter()

    def resolve_application(name: str) -> str | None:
        registered = aliases.get("application", name)
        if registered:
            return registered
        if _is_browser_reference(name):
            preferred = _preferred_registered_browser(aliases, memory_store)
            if preferred:
                return preferred
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
    registry.register(ChanceSkill(logic_state, WindowsNotificationAdapter()))
    registry.register(ProcessSkill(WindowsProcessAdapter()))
    registry.register(InputAutomationSkill(WindowsInputAutomationAdapter()))
    registry.register(WindowsDeviceControlSkill(LocalWindowsDeviceAdapter()))
    registry.register(WindowsSettingSkill(LocalWindowsSettingsAdapter()))


def _register_productivity_skills(
    registry: SkillRegistry,
    database: Database,
    memory_store: MemoryStore,
    schedule_store: ScheduleStore,
    default_timezone: str,
    macro_store: MacroStore,
    playbook_store: PlaybookStore,
) -> None:
    registry.register(TaskSkill(database))
    registry.register(NoteSkill(database))
    registry.register(ReminderSkill(schedule_store))
    registry.register(
        ScheduleSkill(
            schedule_store,
            default_timezone,
            ScheduleAuthoringService(schedule_store, registry, macro_store),
        )
    )
    registry.register(UnitConversionSkill())
    registry.register(MemorySkill(memory_store))
    registry.register(PlaybookSkill(playbook_store, registry))


def _register_network_skills(
    registry: SkillRegistry,
    settings: Settings,
    secret_vault: SecretVault,
) -> None:
    web_client = WebClient()
    registry.register(WebSearchSkill(web_client, secret_vault.get, settings.search_provider))
    registry.register(NewsSearchSkill(web_client))
    registry.register(WebFetchSkill(web_client))
    registry.register(
        HeadlessReadSkill(PlaywrightHeadlessBrowser(offline_mode=settings.offline_lock))
    )
    registry.register(
        CodexAskSkill(settings.openai_codex_model, offline_lock=settings.offline_lock)
    )


def _google_calendar_skill_factory(secret_vault: SecretVault):
    return lambda: GoogleCalendarSkill(GoogleCalendarAdapter(GoogleTokenManager(secret_vault)))


def _gmail_skill_factory(secret_vault: SecretVault):
    return lambda: GmailSkill(
        GmailAdapter(GoogleTokenManager(secret_vault)),
        account=lambda: secret_vault.get("google_account"),
    )


def _google_drive_skill_factory(secret_vault: SecretVault):
    return lambda: GoogleDriveSkill(GoogleDriveAdapter(GoogleTokenManager(secret_vault)))


def _outlook_mail_skill_factory(secret_vault: SecretVault):
    return lambda: OutlookMailSkill(MicrosoftGraphAdapter(MicrosoftGraphTokenManager(secret_vault)))


def _outlook_calendar_skill_factory(secret_vault: SecretVault):
    return lambda: OutlookCalendarSkill(
        MicrosoftGraphAdapter(MicrosoftGraphTokenManager(secret_vault))
    )


def _onedrive_skill_factory(secret_vault: SecretVault):
    return lambda: OneDriveSkill(MicrosoftGraphAdapter(MicrosoftGraphTokenManager(secret_vault)))


def _slack_skill_factory(secret_vault: SecretVault):
    return lambda: SlackSkill(SlackAdapter(ApiTokenProvider(SlackConnectionService(secret_vault))))


def _notion_skill_factory(secret_vault: SecretVault):
    return lambda: NotionSkill(
        NotionAdapter(ApiTokenProvider(NotionConnectionService(secret_vault)))
    )


def _github_skill_factory(secret_vault: SecretVault):
    return lambda: GitHubSkill(
        GitHubAdapter(ApiTokenProvider(GitHubConnectionService(secret_vault)))
    )


def _migrate_cloud_api_key(
    secret_vault: SecretVault,
    target_name: str,
    legacy_names: tuple[str, ...],
) -> None:
    if secret_vault.get(target_name):
        return
    for legacy_name in legacy_names:
        value = secret_vault.get(legacy_name)
        if value:
            secret_vault.set(target_name, value)
            secret_vault.delete(legacy_name)
            return


def _managed_extension_registry(
    secret_vault: SecretVault,
    *,
    openai_model: str = "gpt-5.5",
    codex_model: str = DEFAULT_CODEX_MODEL,
    anthropic_model: str = "claude-sonnet-4-6",
    offline_lock: bool = False,
    pricing: CloudPricing | None = None,
) -> ManagedExtensionRegistry:
    _migrate_cloud_api_key(secret_vault, OPENAI_API_KEY_SECRET, OPENAI_LEGACY_SECRET_NAMES)
    _migrate_cloud_api_key(
        secret_vault,
        ANTHROPIC_API_KEY_SECRET,
        ANTHROPIC_LEGACY_SECRET_NAMES,
    )
    google_calendar_factory = _google_calendar_skill_factory(secret_vault)
    gmail_factory = _gmail_skill_factory(secret_vault)
    google_drive_factory = _google_drive_skill_factory(secret_vault)
    outlook_mail_factory = _outlook_mail_skill_factory(secret_vault)
    outlook_calendar_factory = _outlook_calendar_skill_factory(secret_vault)
    onedrive_factory = _onedrive_skill_factory(secret_vault)
    slack_factory = _slack_skill_factory(secret_vault)
    notion_factory = _notion_skill_factory(secret_vault)
    github_factory = _github_skill_factory(secret_vault)
    google_connection = GoogleConnectionService(secret_vault)
    microsoft_graph_connection = MicrosoftGraphConnectionService(secret_vault)
    slack_connection = SlackConnectionService(secret_vault)
    notion_connection = NotionConnectionService(secret_vault)
    github_connection = GitHubConnectionService(secret_vault)
    openai_connection = OpenAIConnectionService(
        secret_vault,
        offline_lock=offline_lock,
        pricing=pricing,
    )
    anthropic_connection = AnthropicConnectionService(
        secret_vault,
        offline_lock=offline_lock,
        pricing=pricing,
    )

    def openai_factory():
        return OpenAISkill(
            openai_connection.adapter,
            default_model=openai_model,
            default_codex_model=codex_model,
            anthropic_adapter=anthropic_connection.adapter,
            default_anthropic_model=anthropic_model,
            offline_lock=offline_lock,
            pricing=pricing,
        )

    def anthropic_factory():
        return AnthropicSkill(
            anthropic_connection.adapter,
            default_model=anthropic_model,
            openai_adapter=openai_connection.adapter,
            default_openai_model=openai_model,
            default_codex_model=codex_model,
            offline_lock=offline_lock,
            pricing=pricing,
        )

    return ManagedExtensionRegistry(
        (
            ManagedExtensionSpec.from_factory(
                google_calendar_factory,
                package_slug="google-calendar",
                featured=True,
                connection=ConnectionBinding(
                    service=google_connection,
                    provider_id=google_connection.provider_id,
                    required_scopes=(CALENDAR_SCOPE,),
                    dialog_entry_point=(
                        "assistant.ui.desktop.google_calendar_connection:"
                        "GoogleCalendarConnectionDialog"
                    ),
                ),
            ),
            ManagedExtensionSpec.from_factory(
                gmail_factory,
                package_slug="gmail",
                featured=True,
                connection=ConnectionBinding(
                    service=google_connection,
                    provider_id=google_connection.provider_id,
                    required_scopes=(GMAIL_SCOPE,),
                    dialog_entry_point=(
                        "assistant.ui.desktop.gmail_connection:GmailConnectionDialog"
                    ),
                ),
            ),
            ManagedExtensionSpec.from_factory(
                google_drive_factory,
                package_slug="google-drive",
                featured=True,
                connection=ConnectionBinding(
                    service=google_connection,
                    provider_id=google_connection.provider_id,
                    required_scopes=(DRIVE_READ_SCOPE, DRIVE_FILE_SCOPE),
                    dialog_entry_point=(
                        "assistant.ui.desktop.google_drive_connection:GoogleDriveConnectionDialog"
                    ),
                ),
            ),
            ManagedExtensionSpec.from_factory(
                outlook_mail_factory,
                package_slug="outlook-mail",
                featured=True,
                connection=ConnectionBinding(
                    service=microsoft_graph_connection,
                    provider_id=microsoft_graph_connection.provider_id,
                    required_scopes=(
                        OUTLOOK_MAIL_READ_SCOPE,
                        OUTLOOK_MAIL_WRITE_SCOPE,
                        OUTLOOK_MAIL_SEND_SCOPE,
                    ),
                    dialog_entry_point=(
                        "assistant.ui.desktop.microsoft_graph_connection:"
                        "MicrosoftGraphConnectionDialog"
                    ),
                ),
            ),
            ManagedExtensionSpec.from_factory(
                outlook_calendar_factory,
                package_slug="outlook-calendar",
                featured=True,
                connection=ConnectionBinding(
                    service=microsoft_graph_connection,
                    provider_id=microsoft_graph_connection.provider_id,
                    required_scopes=(
                        OUTLOOK_CALENDAR_READ_SCOPE,
                        OUTLOOK_CALENDAR_WRITE_SCOPE,
                    ),
                    dialog_entry_point=(
                        "assistant.ui.desktop.microsoft_graph_connection:"
                        "MicrosoftGraphConnectionDialog"
                    ),
                ),
            ),
            ManagedExtensionSpec.from_factory(
                onedrive_factory,
                package_slug="onedrive",
                featured=True,
                connection=ConnectionBinding(
                    service=microsoft_graph_connection,
                    provider_id=microsoft_graph_connection.provider_id,
                    required_scopes=(ONEDRIVE_READ_SCOPE, ONEDRIVE_WRITE_SCOPE),
                    dialog_entry_point=(
                        "assistant.ui.desktop.microsoft_graph_connection:"
                        "MicrosoftGraphConnectionDialog"
                    ),
                ),
            ),
            ManagedExtensionSpec.from_factory(
                slack_factory,
                package_slug="slack",
                featured=True,
                connection=ConnectionBinding(
                    service=slack_connection,
                    provider_id=slack_connection.provider_id,
                    dialog_entry_point="assistant.ui.desktop.slack_connection:SlackConnectionDialog",
                ),
            ),
            ManagedExtensionSpec.from_factory(
                notion_factory,
                package_slug="notion",
                featured=True,
                connection=ConnectionBinding(
                    service=notion_connection,
                    provider_id=notion_connection.provider_id,
                    dialog_entry_point="assistant.ui.desktop.notion_connection:NotionConnectionDialog",
                ),
            ),
            ManagedExtensionSpec.from_factory(
                github_factory,
                package_slug="github",
                featured=True,
                connection=ConnectionBinding(
                    service=github_connection,
                    provider_id=github_connection.provider_id,
                    dialog_entry_point="assistant.ui.desktop.github_connection:GitHubConnectionDialog",
                ),
            ),
            ManagedExtensionSpec.from_factory(
                openai_factory,
                package_slug="openai",
                featured=True,
                connection=ConnectionBinding(
                    service=openai_connection,
                    provider_id=openai_connection.provider_id,
                    required_scopes=(),
                    connection_required=False,
                    dialog_entry_point=(
                        "assistant.ui.desktop.openai_connection:OpenAIConnectionDialog"
                    ),
                ),
            ),
            ManagedExtensionSpec.from_factory(
                anthropic_factory,
                package_slug="anthropic",
                featured=True,
                connection=ConnectionBinding(
                    service=anthropic_connection,
                    provider_id=anthropic_connection.provider_id,
                    required_scopes=(),
                    dialog_entry_point=(
                        "assistant.ui.desktop.anthropic_connection:AnthropicConnectionDialog"
                    ),
                ),
            ),
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
    playbook_store: PlaybookStore
    memory_store: MemoryStore
    vector_index: VectorIndex
    secret_vault: SecretVault
    connection_profiles: ConnectionProfileStore
    logic_state: LogicStateStore
    schedule_store: ScheduleStore
    notification_store: NotificationStore


@dataclass
class _RetrievalServices:
    embedding_provider: EmbeddingProvider
    semantic_retriever: SemanticRetriever
    skill_indexer: SkillSemanticIndexer
    knowledge_indexer: KnowledgeSemanticIndexer
    memory_indexer: MemorySemanticIndexer
    workflow_indexer: WorkflowTemplateSemanticIndexer


@dataclass
class _AudioServices:
    capture: AudioCapture
    focus: AudioFocusCoordinator
    stt: FasterWhisperProvider
    wakeword: OpenWakeWordProvider | None
    model_manager: ModelManager
    tts: TTSProvider | None


def _build_storage_services(settings: Settings, database: Database) -> _StorageServices:
    audit = AuditStore(database)
    metrics = MetricsStore(database)
    settings_store = SettingsStore(database)
    file_access_policy = FileAccessPolicyStore(settings_store)
    secret_vault = SecretVault(settings.data_dir / "secrets")
    return _StorageServices(
        audit=audit,
        metrics=metrics,
        file_access_policy=file_access_policy,
        permissions=PermissionManager(database, audit, file_access_policy),
        aliases=AliasStore(database),
        knowledge_bases=KnowledgeBaseStore(database),
        playbook_store=PlaybookStore(settings.data_dir / "playbooks"),
        memory_store=MemoryStore(database),
        vector_index=VectorIndex(database),
        secret_vault=secret_vault,
        connection_profiles=ConnectionProfileStore(settings_store, secret_vault),
        logic_state=LogicStateStore(database),
        schedule_store=ScheduleStore(database, audit=audit),
        notification_store=NotificationStore(database),
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
        embedding_provider=embedding_provider,
        semantic_retriever=SemanticRetriever(storage.vector_index, embedding_provider),
        skill_indexer=SkillSemanticIndexer(storage.vector_index, embedding_provider),
        knowledge_indexer=KnowledgeSemanticIndexer(storage.vector_index, embedding_provider),
        memory_indexer=MemorySemanticIndexer(storage.vector_index, embedding_provider),
        workflow_indexer=WorkflowTemplateSemanticIndexer(
            storage.vector_index,
            embedding_provider,
            playbook_store=storage.playbook_store,
        ),
    )


def _build_registry(
    settings: Settings,
    database: Database,
    storage: _StorageServices,
    retrieval: _RetrievalServices,
    events: EventBus,
    managed_extensions: ManagedExtensionRegistry,
) -> tuple[SkillRegistry, MacroStore, MediaSessionRegistry, RemoteMediaCache]:
    registry = SkillRegistry(
        settings.data_dir / "skills",
        trusted_signing_keys=_decode_trusted_keys(settings.trusted_skill_keys),
        revoked_signing_key_ids=set(settings.revoked_skill_key_ids),
        allow_reviewed_worker_extensions=settings.allow_reviewed_worker_extensions,
        managed_skill_factories=managed_extensions.skill_factories,
    )
    _register_core_skills(registry, storage.file_access_policy, storage.logic_state)
    _register_windows_skills(
        registry,
        storage.aliases,
        storage.memory_store,
        storage.logic_state,
    )
    registry.register(LocalDocumentSkill())
    registry.register(
        DocumentContentSearchSkill(storage.vector_index, retrieval.embedding_provider)
    )
    registry.register(
        VisualUnderstandingSkill(
            vision_provider=(
                OllamaVisionProvider(
                    settings.vision_model,
                    timeout_seconds=settings.vision_timeout_seconds,
                )
                if settings.vision_model
                else None
            )
        )
    )
    media_sessions = MediaSessionRegistry()
    media_resolver = MediaResolver()
    media_cache = RemoteMediaCache(
        settings.data_dir / "media-cache",
        offline_lock=settings.offline_lock,
        max_download_bytes=settings.remote_media_max_bytes,
        max_cache_bytes=settings.media_cache_max_bytes,
        retention_days=settings.media_cache_retention_days,
        timeout_seconds=settings.remote_media_timeout_seconds,
    )
    media_cache.prune()
    registry.register(
        MediaPresentSkill(
            media_resolver,
            media_sessions,
            media_cache,
            events,
        )
    )
    registry.register(MediaViewerControlSkill(media_sessions))
    macro_store = MacroStore(database)
    _register_productivity_skills(
        registry,
        database,
        storage.memory_store,
        storage.schedule_store,
        settings.default_timezone,
        macro_store,
        storage.playbook_store,
    )
    _register_network_skills(registry, settings, storage.secret_vault)
    registry.register(MacroSkill(macro_store, registry, storage.permissions, storage.audit))
    registry.register(HelpSkill(registry.list))
    return registry, macro_store, media_sessions, media_cache


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


def _build_audio_services(
    settings: Settings,
    secret_vault: SecretVault | None = None,
) -> _AudioServices:
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
    focus = AudioFocusCoordinator(capture.set_playback_active)
    stt = FasterWhisperProvider(settings.stt_model, settings.stt_device)
    wakeword = (
        OpenWakeWordProvider(
            settings.wake_word_model,
            threshold=settings.wake_word_threshold,
            device=settings.audio_input_device,
        )
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
        settings.tts_provider == "piper"
        and settings.piper_executable
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
            playback_state=focus.output_callback("tts"),
        )
    elif settings.tts_provider == "elevenlabs" and settings.elevenlabs_voice_id:
        tts = ElevenLabsProvider(
            lambda: secret_vault.get("elevenlabs") if secret_vault else None,
            settings.elevenlabs_voice_id,
            settings.elevenlabs_model_id,
            settings.tts_output_device,
            volume=settings.tts_volume,
            timeout_seconds=settings.elevenlabs_timeout_seconds,
            cache_dir=settings.data_dir / "tts-cache",
            playback_state=focus.output_callback("tts"),
            offline_lock=settings.offline_lock,
        )
    return _AudioServices(capture, focus, stt, wakeword, model_manager, tts)


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_url)
    database.migrate()
    settings = _settings_with_persisted_values(settings, database)
    pricing = CloudPricing.from_mapping(settings.cloud_pricing) if settings.cloud_pricing else None
    configure_logging(
        settings.data_dir,
        developer_mode=settings.profile == "development",
    )

    storage = _build_storage_services(settings, database)
    retrieval = _build_retrieval_services(settings, storage)
    events = EventBus()
    managed_extensions = _managed_extension_registry(
        storage.secret_vault,
        openai_model=settings.openai_model,
        codex_model=settings.openai_codex_model,
        anthropic_model=settings.anthropic_model,
        offline_lock=settings.offline_lock,
        pricing=pricing,
    )
    registry, macro_store, media_sessions, media_cache = _build_registry(
        settings,
        database,
        storage,
        retrieval,
        events,
        managed_extensions,
    )
    extension_hub = ExtensionHubService(
        registry,
        ExtensionPlatformClient(
            settings.extension_catalog_url,
            timeout_seconds=settings.extension_catalog_timeout_seconds,
            package_max_bytes=settings.extension_package_max_bytes,
        ),
        audit=storage.audit,
        managed_registry=managed_extensions,
    )
    extension_hub.set_offline_lock(settings.offline_lock)
    extension_hub.set_cloud_pricing(pricing)
    context_providers = _build_context_providers(settings, storage)
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
    notification_center = NotificationCenter(storage.notification_store)
    command_center_registry = CommandCenterRegistry()
    register_builtin_command_center_contributions(command_center_registry)
    command_center_observations = CommandCenterObservationStore(database)
    command_center = CommandCenterService(
        command_center_registry,
        CommandCenterBindingStore(SettingsStore(database)),
        registry,
        storage.connection_profiles,
        engine,
        notification_center,
        CommandCenterArtifactStore(database),
        storage.audit,
        command_center_observations,
    )
    command_center_authoring = ScheduleAuthoringService(
        storage.schedule_store,
        registry,
        macro_store,
    )
    registry.register(WorkBriefSkill(command_center))
    scheduler = SchedulerService(
        storage.schedule_store,
        BackgroundRunner(
            storage.schedule_store,
            notification_center,
            storage.audit,
            metrics=storage.metrics,
            engine=engine,
            registry=registry,
            logic_state=storage.logic_state,
            command_center=command_center,
            background_actions_enabled=settings.background_actions_enabled,
            pause_while_locked=settings.pause_background_while_locked,
            notify_on_success=settings.notify_on_background_success,
            notify_on_failure=settings.notify_on_background_failure,
        ),
        enabled=settings.scheduler_enabled,
        max_concurrency=settings.max_background_concurrency,
        event_sources=(
            FileEventSource(storage.schedule_store, storage.file_access_policy),
            ProcessEventSource(storage.schedule_store, WindowsProcessAdapter()),
            DeviceEventSource(storage.schedule_store, LocalWindowsDeviceAdapter()),
            WebEventSource(storage.schedule_store),
            CommandCenterEventSource(
                storage.schedule_store,
                command_center,
                command_center_observations,
                offline_lock=lambda: settings.offline_lock,
            ),
        ),
    )
    if settings.automations_paused:
        scheduler.pause()
    background_runtime = BackgroundRuntime(scheduler)
    dashboard_registry = DashboardProviderRegistry()
    dashboard_registry.register(AssistantMetricsProvider(storage.metrics, settings))
    dashboard_registry.register(AutomationMetricsProvider(scheduler, storage.metrics))
    dashboard_registry.register(TaskMetricsProvider(database))
    dashboard_registry.register(WindowsDeviceMetricProvider(LocalWindowsDeviceAdapter()))
    dashboard_registry.register(HomeAssistantMetricProvider(storage.secret_vault))
    dashboard = DashboardService(
        dashboard_registry,
        DashboardLayoutStore(SettingsStore(database), default_dashboard_layout),
        permissions=storage.permissions,
        connections=DashboardConnectionStore(
            SettingsStore(database),
            storage.secret_vault,
        ),
        samples=DashboardSampleStore(database),
        audit=storage.audit,
        offline_lock=settings.offline_lock,
        history_retention_days=settings.dashboard_history_retention_days,
    )
    llm = (
        OllamaProvider(
            settings.llm_model,
            keep_alive=settings.llm_keep_alive,
            usage_callback=lambda usage: _record_ollama_usage(storage.metrics, usage),
        )
        if settings.llm_provider == "ollama"
        else None
    )
    planner = (
        LocalPlanner(
            llm,
            SkillRetriever(
                lambda: tuple(
                    manifest
                    for manifest in registry.list()
                    if registry.is_enabled(manifest.skill_id)
                )
            ),
            storage.knowledge_bases,
            timeout_seconds=settings.planner_timeout_seconds,
            response_detail=settings.response_detail,
            argument_validator=lambda skill_id, arguments: registry.get(skill_id).validate(
                arguments
            ),
        )
        if llm
        else None
    )
    if llm is not None and settings.llm_preload and settings.profile != "testing":
        llm.preload_in_background(
            timeout_seconds=min(15.0, settings.planner_timeout_seconds),
        )
    orchestrator = AssistantOrchestrator(
        DeterministicRouter(
            application_names=lambda: storage.aliases.all("application"),
            skill_status=extension_hub.skill_availability,
        ),
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
        playbook_store=storage.playbook_store,
        context_providers=context_providers,
        media_sessions=media_sessions,
    )
    audio = _build_audio_services(settings, storage.secret_vault)
    orchestrator.tts = audio.tts
    orchestrator.speech_mode = settings.speech_mode
    container = Container(
        settings=settings,
        database=database,
        audit=storage.audit,
        permissions=storage.permissions,
        confirmations=confirmations,
        registry=registry,
        extension_hub=extension_hub,
        events=events,
        engine=engine,
        orchestrator=orchestrator,
        audio_capture=audio.capture,
        audio_focus=audio.focus,
        stt=audio.stt,
        macro_store=macro_store,
        wakeword=audio.wakeword,
        model_manager=audio.model_manager,
        metrics=storage.metrics,
        aliases=storage.aliases,
        knowledge_bases=storage.knowledge_bases,
        playbook_store=storage.playbook_store,
        file_access_policy=storage.file_access_policy,
        vector_index=storage.vector_index,
        semantic_retriever=retrieval.semantic_retriever,
        knowledge_semantic_indexer=retrieval.knowledge_indexer,
        memory_semantic_indexer=retrieval.memory_indexer,
        workflow_template_indexer=retrieval.workflow_indexer,
        memory_store=storage.memory_store,
        logic_state=storage.logic_state,
        schedule_store=storage.schedule_store,
        notification_store=storage.notification_store,
        notification_center=notification_center,
        scheduler=scheduler,
        background_runtime=background_runtime,
        dashboard=dashboard,
        command_center=command_center,
        command_center_authoring=command_center_authoring,
        secret_vault=storage.secret_vault,
        connection_profiles=storage.connection_profiles,
        media_sessions=media_sessions,
        media_cache=media_cache,
    )
    registry.register(
        AssistantSettingSkill(
            ApplicationSettingsService(
                SettingsStore(database),
                lambda: container.settings,
                lambda updated: _apply_application_settings(container, updated),
            )
        )
    )
    registry.register(
        DiagnosticSkill(
            registry,
            {
                "stt": audio.stt,
                "llm": llm,
                "tts": audio.tts,
                "scheduler": scheduler,
            },
            media_cache=media_cache,
            media_sessions=media_sessions,
        )
    )
    for skill_id, error in registry.load_all_installed().items():
        logging.getLogger(__name__).warning(
            "Installed extension could not be loaded",
            extra={"skill_id": skill_id, "outcome": error},
        )
    extension_hub.sync_connected_extensions()
    return container
