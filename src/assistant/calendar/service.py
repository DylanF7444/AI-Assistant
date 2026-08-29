from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from assistant.calendar.contracts import (
    CalendarLayout,
    CalendarPermissionRequest,
    CalendarProviderDefinition,
    CalendarQuery,
    CalendarSnapshot,
    CalendarState,
)
from assistant.calendar.registry import CalendarProvider, CalendarProviderRegistry
from assistant.calendar.storage import CalendarLayoutStore
from assistant.policy.permissions import GrantType, PermissionManager
from assistant.storage.database import SettingsStore


class CalendarService:
    """Permission-aware, transient calendar read service.

    Calendar event bodies are intentionally never written to dashboard history,
    audit payloads, or this service's persistent storage. The in-memory last-good
    snapshot only supports a useful stale/offline display while the app is open.
    """

    def __init__(
        self,
        registry: CalendarProviderRegistry,
        settings: SettingsStore,
        *,
        permissions: PermissionManager | None = None,
        user_id: str = "local-user",
        session_id: str = "desktop",
        offline_lock: bool = False,
        timeout_seconds: float = 10,
        max_external_concurrency: int = 2,
    ) -> None:
        self.registry = registry
        self.layout_store = CalendarLayoutStore(settings)
        self.permissions = permissions
        self.user_id = user_id
        self.session_id = session_id
        self.offline_lock = offline_lock
        self.timeout_seconds = timeout_seconds
        self.max_external_concurrency = max(1, max_external_concurrency)
        # Calendar refreshes run in short-lived AsyncWorker event loops. A
        # thread semaphore keeps the bound valid across those loops without
        # leaking an asyncio primitive between worker threads.
        self._external_semaphore = threading.BoundedSemaphore(self.max_external_concurrency)
        self._diagnostics_lock = threading.Lock()
        self._refresh_started = 0
        self._state_counts: dict[str, int] = {}
        self._last_state: str | None = None
        self._last_error_code: str | None = None
        self._last_good: dict[str, CalendarSnapshot] = {}

    def layout(self) -> CalendarLayout:
        return self.layout_store.load()

    def save_layout(self, layout: CalendarLayout) -> CalendarLayout:
        validated = CalendarLayout.model_validate(layout)
        self.registry.provider(validated.provider_id)  # Fail closed on unknown providers.
        return self.layout_store.save(validated)

    def provider_definitions(self) -> tuple[CalendarProviderDefinition, ...]:
        return self.registry.definitions()

    def register_provider(self, provider: CalendarProvider) -> None:
        """Register an application-owned provider before it is selected by the UI."""
        self.registry.register(provider)

    def diagnostics(self) -> dict[str, int | str | None]:
        with self._diagnostics_lock:
            counts = dict(self._state_counts)
            return {
                "providers": len(self.provider_definitions()),
                "refresh_started": self._refresh_started,
                "last_state": self._last_state,
                "last_error": self._last_error_code,
                **{state.value: counts.get(state.value, 0) for state in CalendarState},
            }

    def set_offline_lock(self, enabled: bool) -> None:
        self.offline_lock = bool(enabled)

    def query(
        self,
        *,
        start: datetime,
        end: datetime,
        layout: CalendarLayout | None = None,
    ) -> CalendarQuery:
        selected = layout or self.layout()
        return CalendarQuery(
            provider_id=selected.provider_id,
            calendar_ids=selected.calendar_ids,
            start=start,
            end=end,
            max_events=selected.max_events,
            show_declined=selected.show_declined,
            # Offset abbreviations such as "CDT" are not portable API
            # identifiers. The provider receives explicit RFC3339 bounds and
            # may add an IANA timezone when its API needs one.
            time_zone=None,
        )

    def permission_request(self, layout: CalendarLayout | None = None) -> CalendarPermissionRequest:
        selected = layout or self.layout()
        query = CalendarQuery(
            provider_id=selected.provider_id,
            calendar_ids=selected.calendar_ids,
            start=datetime.now(UTC),
            end=datetime.now(UTC) + timedelta(minutes=1),
            max_events=selected.max_events,
            show_declined=selected.show_declined,
        )
        provider = self.registry.provider(selected.provider_id)
        scopes = provider.required_capabilities(query)
        if not scopes:
            raise ValueError(f"{provider.definition.display_name} does not require permission")
        return CalendarPermissionRequest(
            provider_id=selected.provider_id,
            display_name=provider.definition.display_name,
            subject_id=provider.permission_subject_for(query),
            calendar_ids=selected.calendar_ids,
            scopes=scopes,
        )

    def grant_permissions(
        self,
        grant_type: GrantType,
        layout: CalendarLayout | None = None,
    ) -> None:
        if self.permissions is None:
            raise RuntimeError("Calendar permission service is unavailable")
        request = self.permission_request(layout)
        for scope in request.scopes:
            self.permissions.grant(
                user_id=self.user_id,
                skill_id=request.subject_id,
                capability=scope.capability,
                grant_type=grant_type,
                targets=scope.targets,
                session_id=self.session_id if grant_type == GrantType.SESSION else None,
                correlation_id=str(uuid4()),
            )

    async def refresh(self, query: CalendarQuery) -> CalendarSnapshot:
        with self._diagnostics_lock:
            self._refresh_started += 1
        query = CalendarQuery.model_validate(query)
        try:
            provider = self.registry.provider(query.provider_id)
            provider.validate_query(query)
        except Exception as error:
            return self._record_snapshot(
                CalendarSnapshot(
                    provider_id=query.provider_id,
                    source_label=query.provider_id,
                    range_start=query.start,
                    range_end=query.end,
                    state=CalendarState.ERROR,
                    summary="The selected calendar provider is unavailable.",
                    error_code=type(error).__name__.lower(),
                )
            )
        key = self._cache_key(query)
        if provider.definition.remote and self.offline_lock:
            return self._record_snapshot(self._offline_snapshot(query, key, provider.definition))

        try:
            status = provider.status(query)
            if status.state != CalendarState.READY:
                return self._record_snapshot(
                    CalendarSnapshot(
                        provider_id=query.provider_id,
                        source_label=provider.definition.source_label,
                        range_start=query.start,
                        range_end=query.end,
                        state=status.state,
                        summary=status.message,
                        account=status.account,
                        error_code=status.error_code,
                    )
                )

            required = provider.required_capabilities(query)
            if required:
                if self.permissions is None:
                    return self._permission_snapshot(query, provider.definition)
                decision = self.permissions.authorize_scopes(
                    user_id=self.user_id,
                    subject_id=provider.permission_subject_for(query),
                    session_id=self.session_id,
                    required=required,
                    consume_once=True,
                )
                if not decision.allowed:
                    return self._record_snapshot(
                        self._permission_snapshot(query, provider.definition, decision.reason)
                    )

            with self._external_semaphore:
                events = await asyncio.wait_for(
                    provider.collect(query),
                    timeout=self.timeout_seconds,
                )
            ordered = tuple(
                sorted(
                    events,
                    key=lambda item: (
                        item.sort_at,
                        item.calendar_id.casefold(),
                        item.title.casefold(),
                    ),
                )
            )
            snapshot = CalendarSnapshot(
                provider_id=query.provider_id,
                source_label=provider.definition.source_label,
                range_start=query.start,
                range_end=query.end,
                state=CalendarState.READY if ordered else CalendarState.EMPTY,
                events=ordered,
                summary=(
                    f"{len(ordered)} event(s) in the selected range."
                    if ordered
                    else "No events in the selected range."
                ),
                account=status.account,
            )
            if snapshot.state in {CalendarState.READY, CalendarState.EMPTY}:
                self._last_good[key] = snapshot
            return self._record_snapshot(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            previous = self._last_good.get(key)
            if previous is not None:
                return self._record_snapshot(
                    previous.model_copy(
                        update={
                            "state": CalendarState.STALE,
                            "summary": "Calendar refresh failed. Showing the last refreshed events.",
                            "error_code": type(error).__name__.lower(),
                        }
                    )
                )
            return self._record_snapshot(
                CalendarSnapshot(
                    provider_id=query.provider_id,
                    source_label=provider.definition.source_label,
                    range_start=query.start,
                    range_end=query.end,
                    state=CalendarState.ERROR,
                    summary="Calendar could not be refreshed.",
                    error_code=type(error).__name__.lower(),
                )
            )

    def _record_snapshot(self, snapshot: CalendarSnapshot) -> CalendarSnapshot:
        state = snapshot.state.value
        with self._diagnostics_lock:
            self._state_counts[state] = self._state_counts.get(state, 0) + 1
            self._last_state = state
            self._last_error_code = snapshot.error_code
        return snapshot

    @staticmethod
    def _cache_key(query: CalendarQuery) -> str:
        return json.dumps(query.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _permission_snapshot(
        query: CalendarQuery,
        definition: CalendarProviderDefinition,
        reason: str | None = None,
    ) -> CalendarSnapshot:
        return CalendarSnapshot(
            provider_id=query.provider_id,
            source_label=definition.source_label,
            range_start=query.start,
            range_end=query.end,
            state=CalendarState.PERMISSION_REQUIRED,
            summary=reason or "Calendar access is required before events can load.",
            error_code="permission_required",
        )

    def _offline_snapshot(
        self,
        query: CalendarQuery,
        key: str,
        definition: CalendarProviderDefinition,
    ) -> CalendarSnapshot:
        previous = self._last_good.get(key)
        if previous is not None:
            return previous.model_copy(
                update={
                    "state": CalendarState.OFFLINE,
                    "summary": "Offline lock is enabled. Showing the last refreshed events.",
                    "error_code": "offline",
                }
            )
        return CalendarSnapshot(
            provider_id=query.provider_id,
            source_label=definition.source_label,
            range_start=query.start,
            range_end=query.end,
            state=CalendarState.OFFLINE,
            summary="The global offline lock is enabled.",
            error_code="offline",
        )
