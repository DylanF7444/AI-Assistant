from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assistant.contracts import Intent
from assistant.router.types import RouteContext, RouteResult
from assistant.settings_service import (
    parse_assistant_setting_value,
    resolve_assistant_setting_name,
)

if TYPE_CHECKING:
    from assistant.router.deterministic import DeterministicRouter


class AssistantSettingsHandler:
    def route(
        self,
        context: RouteContext,
        router: DeterministicRouter,
    ) -> RouteResult | None:
        routed = context.routed
        if match := re.fullmatch(
            r"(?:turn|switch) (?P<setting>.+?) (?P<state>on|off)",
            routed,
        ):
            return self._set(match.group("setting"), match.group("state"))
        if match := re.fullmatch(
            r"(?P<state>enable|disable) (?:the )?(?P<setting>.+?)(?: setting)?",
            routed,
        ):
            return self._set(match.group("setting"), match.group("state"))
        if match := re.fullmatch(
            r"(?:set|change) (?:the )?(?P<setting>.+?)(?: setting)? "
            r"(?:to|as) (?P<value>.+)",
            routed,
        ):
            return self._set(match.group("setting"), match.group("value"))
        if match := re.fullmatch(
            r"(?:toggle|switch) (?:the )?(?P<setting>.+?)(?: setting)?",
            routed,
        ):
            setting = resolve_assistant_setting_name(match.group("setting"))
            if setting is not None:
                return self._result("toggle", setting)
        if match := re.fullmatch(
            r"(?:what is|show|check) (?:the )?(?P<setting>.+?)(?: setting)?",
            routed,
        ):
            setting = resolve_assistant_setting_name(match.group("setting"))
            if setting is not None:
                return self._result("get", setting)
        return None

    def _set(self, setting_text: str, value_text: str) -> RouteResult | None:
        setting = resolve_assistant_setting_name(setting_text)
        if setting is None:
            return None
        return self._result(
            "set",
            setting,
            value=parse_assistant_setting_value(value_text),
        )

    @staticmethod
    def _result(operation: str, setting: str, **arguments: object) -> RouteResult:
        return RouteResult(
            Intent(
                name="core.settings",
                arguments={"operation": operation, "setting": setting, **arguments},
                confidence=0.96,
                source="rule",
            )
        )
