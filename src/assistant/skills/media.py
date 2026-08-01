from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant.contracts import (
    BackgroundMode,
    MediaAction,
    MediaDirective,
    MediaKind,
    MediaSource,
    PermissionScope,
    PipelineEvent,
    PipelineState,
    RequestResultItem,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillError,
    SkillManifest,
    SkillResult,
)
from assistant.events import EventBus
from assistant.presentation.media_resolver import MediaResolution, MediaResolver
from assistant.presentation.remote_media import (
    RemoteMediaCache,
    is_explicit_remote_media_url,
    sanitized_media_url,
)
from assistant.presentation.session import MediaSessionRegistry
from assistant.skills.base import Skill


class MediaPresentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    query: str | None = Field(default=None, min_length=1, max_length=500)
    root: str | None = None
    kind_hint: MediaKind | None = None
    autoplay: bool = False
    start_seconds: float | None = Field(default=None, ge=0)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def _source_or_search_must_be_exact(self) -> MediaPresentInput:
        has_source = self.source is not None
        has_root = self.root is not None
        if has_source == has_root:
            raise ValueError("Provide either one exact source or one search root")
        if self.query is not None and not has_root:
            raise ValueError("A media filename query requires a search root")
        return self


class MediaPresentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    presentation_id: str | None = None
    source: MediaSource | None = None
    paths: list[str] = Field(default_factory=list)


class MediaViewerControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "play",
        "pause",
        "toggle_playback",
        "stop",
        "seek_relative",
        "seek_absolute",
        "set_volume",
        "set_rate",
        "toggle_mute",
        "fullscreen",
        "exit_fullscreen",
        "close",
    ]
    position_seconds: float | None = Field(default=None, ge=0)
    delta_seconds: float | None = None
    volume: float | None = Field(default=None, ge=0, le=1)
    playback_rate: float | None = Field(default=None, ge=0.25, le=4)

    @model_validator(mode="after")
    def _control_fields_must_match_action(self) -> MediaViewerControlInput:
        MediaDirective(
            presentation_id=uuid4(),
            action=MediaAction(self.action),
            position_seconds=self.position_seconds,
            delta_seconds=self.delta_seconds,
            volume=self.volume,
            playback_rate=self.playback_rate,
        )
        return self


class MediaViewerControlOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    presentation_id: str
    action: MediaAction


class MediaPresentSkill(Skill):
    input_model = MediaPresentInput
    output_model = MediaPresentOutput

    def __init__(
        self,
        resolver: MediaResolver,
        sessions: MediaSessionRegistry,
        remote_cache: RemoteMediaCache | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.resolver = resolver
        self.sessions = sessions
        self.remote_cache = remote_cache
        self.events = events
        self.manifest = SkillManifest(
            skill_id="media.present",
            version="1.0.0",
            display_name="Media Presentation",
            category="Media",
            description=(
                "Show an approved local or explicitly downloaded image, audio file, "
                "or video in the assistant."
            ),
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            permissions=(),
            risk=RiskLevel.LOW,
            entry_point=f"{__name__}:MediaPresentSkill",
            safe_to_retry=False,
            background_mode=BackgroundMode.FOREGROUND_ONLY,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        if args.source is not None and is_explicit_remote_media_url(args.source):
            if self.remote_cache is None:
                raise ValueError("Remote media acquisition is not configured")
            return (
                PermissionScope(
                    capability="network",
                    targets=(self.remote_cache.permission_target(args.source),),
                    reason="Download this explicit public HTTPS media resource.",
                ),
            )
        target = self.resolver.permission_target(
            source=args.source,
            root=args.root,
        )
        return (
            PermissionScope(
                capability="filesystem.read",
                targets=(target,),
                reason=(
                    "Read this exact media file."
                    if args.source is not None
                    else "Browse supported media inside this approved folder."
                ),
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        target = args.source or f"{args.query} in {args.root}"
        if args.source is not None and is_explicit_remote_media_url(args.source):
            target = sanitized_media_url(args.source)
        return f"Show media in the assistant: {target}"

    async def execute(
        self,
        call: SkillCall,
        cancel_event: asyncio.Event,
    ) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        if args.source is not None and is_explicit_remote_media_url(args.source):
            if self.remote_cache is None:
                raise ValueError("Remote media acquisition is not configured")
            if self.events is not None:
                await self.events.publish(
                    PipelineEvent(
                        correlation_id=call.correlation_id,
                        state=PipelineState.EXECUTING,
                        message="Downloading remote media",
                        progress=0,
                    )
                )
            loop = asyncio.get_running_loop()

            def progress(downloaded: int, total: int | None) -> None:
                if self.events is None:
                    return
                fraction = min(1.0, downloaded / total) if total is not None and total > 0 else None
                event = PipelineEvent(
                    correlation_id=call.correlation_id,
                    state=PipelineState.EXECUTING,
                    message="Downloading remote media",
                    progress=fraction,
                )

                def publish() -> None:
                    loop.create_task(self.events.publish(event))

                loop.call_soon_threadsafe(publish)

            source = await self.remote_cache.acquire(
                args.source,
                cancel_event=cancel_event,
                progress=progress,
            )
            if args.kind_hint is not None and source.kind != args.kind_hint:
                Path(source.location).unlink(missing_ok=True)
                raise ValueError(
                    f"The downloaded file is {source.kind.value} media, "
                    f"not {args.kind_hint.value} media."
                )
            resolution = MediaResolution(source=source)
            if self.events is not None:
                await self.events.publish(
                    PipelineEvent(
                        correlation_id=call.correlation_id,
                        state=PipelineState.EXECUTING,
                        message="Remote media downloaded",
                        progress=1,
                    )
                )
        else:
            resolution = self.resolver.resolve(
                source=args.source,
                query=args.query,
                root=args.root,
                kind_hint=args.kind_hint,
                limit=args.limit,
            )
        if resolution.source is None:
            paths = list(resolution.candidates)
            if paths:
                display = "More than one media file matched:\n" + "\n".join(
                    f"{index}. {path}" for index, path in enumerate(paths, 1)
                )
                message = "Select one of the numbered media results."
            else:
                display = "No supported media files matched that request."
                message = "No supported media files matched."
            return SkillResult(
                status=ResultStatus.DENIED,
                summary=message,
                display_text=display,
                data=MediaPresentOutput(paths=paths).model_dump(mode="json"),
                items=tuple(
                    RequestResultItem(
                        item_id=f"file-{index}",
                        title=path,
                        value=path,
                        kind="file",
                        metadata={"path": path},
                    )
                    for index, path in enumerate(paths, 1)
                ),
                error=SkillError(
                    code="clarification_required",
                    message=message,
                    recoverable=True,
                ),
            )
        snapshot = self.sessions.prepare(resolution.source)
        directive = MediaDirective(
            presentation_id=snapshot.presentation_id,
            action=MediaAction.SHOW,
            source=resolution.source,
            autoplay=args.autoplay,
            position_seconds=args.start_seconds,
        )
        data = MediaPresentOutput(
            presentation_id=str(snapshot.presentation_id),
            source=resolution.source,
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Prepared {resolution.source.title} for the in-app viewer.",
            data=data.model_dump(mode="json"),
            display_text=f"Showing {resolution.source.title}.",
            speech_text=f"Showing {resolution.source.title}.",
            media_directive=directive,
        )


class MediaViewerControlSkill(Skill):
    input_model = MediaViewerControlInput
    output_model = MediaViewerControlOutput

    def __init__(self, sessions: MediaSessionRegistry) -> None:
        self.sessions = sessions
        self.manifest = SkillManifest(
            skill_id="media.viewer_control",
            version="1.0.0",
            display_name="Media Viewer Control",
            category="Media",
            description="Control the currently active in-app media viewer.",
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            permissions=(),
            risk=RiskLevel.LOW,
            entry_point=f"{__name__}:MediaViewerControlSkill",
            safe_to_retry=False,
            background_mode=BackgroundMode.FOREGROUND_ONLY,
        )

    async def execute(
        self,
        call: SkillCall,
        cancel_event: asyncio.Event,
    ) -> SkillResult:
        args = self.validate(call.arguments)
        presentation_id = self.sessions.active_presentation_id()
        if presentation_id is None:
            raise ValueError("There is no active in-app media presentation")
        directive = MediaDirective(
            presentation_id=presentation_id,
            action=MediaAction(args.action),
            position_seconds=args.position_seconds,
            delta_seconds=args.delta_seconds,
            volume=args.volume,
            playback_rate=args.playback_rate,
        )
        if directive.action == MediaAction.CLOSE:
            self.sessions.close(presentation_id)
        data = MediaViewerControlOutput(
            presentation_id=str(presentation_id),
            action=directive.action,
        )
        label = directive.action.value.replace("_", " ").capitalize()
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"{label} requested for the in-app viewer.",
            data=data.model_dump(mode="json"),
            media_directive=directive,
        )
