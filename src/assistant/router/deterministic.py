from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Literal
from urllib.parse import urlencode, urlsplit, urlunsplit

from assistant.contracts import Intent
from assistant.router.handlers import DEFAULT_ROUTE_HANDLERS
from assistant.router.normalization import command_text, normalize_text, spoken_number
from assistant.router.types import RouteContext, RouteResult
from assistant.windows.process_names import executable_from_command_name


class DeterministicRouter:
    def __init__(
        self,
        application_names: Callable[[], Iterable[str]] | Iterable[str] | None = None,
        *,
        skill_status: Callable[[str], Literal["enabled", "disabled", "disconnected", "unavailable"]]
        | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.application_names = application_names
        self._skill_status = skill_status
        self._clock = clock or (lambda: datetime.now().astimezone())

    APP_REGISTRATION_HELP = (
        "Open the Applications tab, choose Register Application, select the app's "
        ".exe file, and give it the name you want to use. After that you can say "
        '"open <name>" and I will use that registered path.'
    )

    CONTROL = {
        "cancel": "control.cancel",
        "cancel that": "control.cancel",
        "cancel this": "control.cancel",
        "stop": "control.cancel",
        "never mind": "control.cancel",
        "never mind that": "control.cancel",
        "nevermind": "control.cancel",
        "nevermind that": "control.cancel",
        "undo": "control.undo",
        "undo last command": "control.undo",
        "undo that": "control.undo",
        "mute": "control.mute",
        "yes": "control.confirm",
        "confirm": "control.confirm",
        "no": "control.deny",
        "deny": "control.deny",
        "help": "core.help",
        "forget this session": "control.forget",
        "forget this conversation": "control.forget",
        "forget everything from this session": "control.forget",
    }
    COMMON_APPLICATIONS = {
        "calculator",
        "command prompt",
        "explorer",
        "file explorer",
        "notepad",
        "paint",
        "powershell",
        "settings",
        "terminal",
        "windows terminal",
    }
    BROWSER_APPLICATIONS = {
        "browser",
        "brave",
        "brave browser",
        "chrome",
        "chromium",
        "google chrome",
        "edge",
        "microsoft edge",
        "firefox",
        "firefox developer edition",
        "firefox nightly",
        "mozilla firefox",
        "librewolf",
        "opera",
        "opera browser",
        "opera gx",
        "vivaldi",
        "vivaldi browser",
        "waterfox",
    }
    WEBSITE_APPLICATIONS = {
        "amazon": "https://www.amazon.com",
        "chat gpt": "https://chatgpt.com",
        "chatgpt": "https://chatgpt.com",
        "facebook": "https://www.facebook.com",
        "github": "https://github.com",
        "gmail": "https://mail.google.com",
        "google": "https://www.google.com",
        "google maps": "https://www.google.com/maps",
        "instagram": "https://www.instagram.com",
        "linkedin": "https://www.linkedin.com",
        "netflix": "https://www.netflix.com",
        "reddit": "https://www.reddit.com",
        "slick deals": "https://slickdeals.net",
        "slickdeals": "https://slickdeals.net",
        "stack overflow": "https://stackoverflow.com",
        "twitch": "https://www.twitch.tv",
        "wikipedia": "https://www.wikipedia.org",
        "youtube": "https://www.youtube.com",
    }

    def route(self, text: str) -> RouteResult:
        context = RouteContext(
            text=text,
            normalized=normalize_text(text),
            routed=command_text(text),
        )
        for handler in DEFAULT_ROUTE_HANDLERS:
            if result := handler.route(context, self):
                return result
        raise RuntimeError("Deterministic router has no fallback handler")

    def skill_availability(
        self, skill_id: str
    ) -> Literal["enabled", "disabled", "disconnected", "unavailable"]:
        if self._skill_status is None:
            return "enabled"
        return self._skill_status(skill_id)

    def now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.astimezone()

    def _registered_application_names(self) -> tuple[str, ...]:
        names = (
            self.application_names() if callable(self.application_names) else self.application_names
        )
        return tuple(
            sorted(
                {
                    name.strip().casefold()
                    for name in (names or ())
                    if isinstance(name, str) and name.strip()
                },
                key=len,
                reverse=True,
            )
        )

    def _is_known_application_reference(self, application: str) -> bool:
        normalized = re.sub(
            r"^(?:my|the)\s+",
            "",
            application.strip().casefold(),
        )
        return (
            normalized in self.BROWSER_APPLICATIONS
            or normalized in self.COMMON_APPLICATIONS
            or normalized in self._registered_application_names()
            or normalized.endswith(".exe")
        )

    @staticmethod
    def _route_file_access_policy(routed: str) -> Intent | None:
        access_words = r"read|reading|write|writing|delete|deleting|write/delete"
        list_words = r"blacklist|whitelist"
        if match := re.fullmatch(
            rf"(?P<operation>add|remove) (?P<path>.+) "
            rf"(?:to|from) (?:the )?(?P<access>{access_words}) "
            rf"(?P<list_name>{list_words})",
            routed,
        ):
            operation = match.group("operation")
            return Intent(
                name="core.file_access_policy",
                arguments={
                    "operation": operation,
                    "path": match.group("path").strip(),
                    "access": DeterministicRouter._policy_access(match.group("access")),
                    "list_name": match.group("list_name"),
                },
                confidence=0.94,
                source="rule",
            )
        if match := re.fullmatch(
            rf"(?P<list_name>{list_words}) (?P<path>.+) "
            rf"for (?P<access>{access_words})(?: access)?",
            routed,
        ):
            return Intent(
                name="core.file_access_policy",
                arguments={
                    "operation": "add",
                    "path": match.group("path").strip(),
                    "access": DeterministicRouter._policy_access(match.group("access")),
                    "list_name": match.group("list_name"),
                },
                confidence=0.94,
                source="rule",
            )
        return None

    @staticmethod
    def _policy_access(value: str) -> str:
        if value in {"write", "writing", "write/delete"}:
            return "write"
        if value in {"delete", "deleting"}:
            return "delete"
        return "read"

    @staticmethod
    def _percent(text: str) -> int | None:
        cleaned = re.sub(r"\b(?:percent|per cent)\b|%", "", text.strip().casefold()).strip()
        if match := re.fullmatch(r"\d{1,3}", cleaned):
            value = int(match.group(0))
        else:
            try:
                spoken = spoken_number(cleaned)
            except ValueError:
                return None
            if not spoken.is_integer():
                return None
            value = int(spoken)
        return value if 0 <= value <= 100 else None

    @staticmethod
    def _route_context_navigation(routed: str) -> Intent | None:
        patterns = (
            (
                "new_tab",
                (
                    r"(?:open|launch|start|create) (?:a |an |the )?(?:new |blank )?(?:browser )?tab",
                    r"(?:new|blank) (?:browser )?tab",
                ),
            ),
            (
                "new_window",
                (
                    r"(?:open|launch|start|create) (?:a |an |the )?(?:new |blank )?(?:browser )?window",
                    r"(?:new|blank) (?:browser )?window",
                ),
            ),
            (
                "close_tab",
                (
                    r"(?:close|shut) (?:this |current |active |the )?(?:browser )?tab",
                    r"close tab",
                ),
            ),
            (
                "reopen_closed_tab",
                (
                    r"(?:reopen|restore) (?:the |my |that )?(?:last |recently )?closed (?:browser )?tab",
                ),
            ),
            (
                "next_tab",
                (
                    r"(?:go to |switch to )?next (?:browser )?tab",
                    r"(?:go|switch) (?:to )?the next (?:browser )?tab",
                ),
            ),
            (
                "previous_tab",
                (
                    r"(?:go to |switch to )?(?:previous|prev|last) (?:browser )?tab",
                    r"(?:go|switch) (?:to )?the (?:previous|prev|last) (?:browser )?tab",
                ),
            ),
            (
                "focus_address_bar",
                (
                    r"(?:focus|select|highlight|open|go to) (?:the )?(?:address bar|url bar|omnibox)",
                ),
            ),
            (
                "back",
                (
                    r"(?:go|navigate) back(?: (?:a|one) page)?",
                    r"back(?: (?:a|one) page)?",
                ),
            ),
            (
                "forward",
                (
                    r"(?:go|navigate) forward(?: (?:a|one) page)?",
                    r"forward(?: (?:a|one) page)?",
                ),
            ),
            (
                "reload",
                (r"(?:reload|refresh)(?: (?:this |the |current )?(?:page|tab|browser))?",),
            ),
        )
        for operation, operation_patterns in patterns:
            if any(re.fullmatch(pattern, routed) for pattern in operation_patterns):
                return Intent(
                    name="windows.context_navigation",
                    arguments={"target": "browser", "operation": operation},
                    confidence=0.93,
                    source="rule",
                )
        return None

    @staticmethod
    def _route_browser_tab_launch(routed: str) -> Intent | None:
        browser_names = "|".join(
            re.escape(name)
            for name in sorted(DeterministicRouter.BROWSER_APPLICATIONS, key=len, reverse=True)
        )
        website_first_intent = DeterministicRouter._route_website_first_private_browser_launch(
            routed, browser_names
        )
        if website_first_intent:
            return website_first_intent
        website_match = re.fullmatch(
            rf"(?:open|launch|start)\s+"
            rf"(?:a |an |the )?"
            rf"(?:new\s+)?"
            rf"(?P<private_before>private|incognito|inprivate)\s+"
            rf"(?P<application>{browser_names})\s+"
            rf"(?:(?:browser)\s+)?"
            rf"(?:tab|window)\s+"
            rf"(?:with|to|for|at)\s+"
            rf"(?P<website>.+)",
            routed,
        )
        if not website_match:
            website_match = re.fullmatch(
                rf"(?:open|launch|start)\s+"
                rf"(?:a |an |the )?"
                rf"(?:new\s+)?"
                rf"(?P<application>{browser_names})\s+"
                rf"(?:(?P<private_after>private|incognito|inprivate)\s+)?"
                rf"(?:(?:browser)\s+)?"
                rf"(?:tab|window)\s+"
                rf"(?:with|to|for|at)\s+"
                rf"(?P<website>.+)",
                routed,
            )
        if not website_match:
            website_match = re.fullmatch(
                rf"(?:open|launch|start)\s+"
                rf"(?:a |an |the )?"
                rf"(?:new\s+)?"
                rf"(?P<private_before>private|incognito|inprivate)\s+"
                rf"(?:(?:browser)\s+)?"
                rf"(?:tab|window)\s+"
                rf"(?:in|on|with|using)\s+"
                rf"(?P<application>{browser_names})\s+"
                rf"(?:with|to|for|at)\s+"
                rf"(?P<website>.+)",
                routed,
            )
        if not website_match:
            website_match = re.fullmatch(
                r"(?:open|launch|start)\s+"
                r"(?:a |an |the )?"
                r"(?:new\s+)?"
                r"(?P<private_before>private|incognito|inprivate)\s+"
                r"(?:(?:browser)\s+)?"
                r"(?:tab|window)\s+"
                r"(?:with|to|for|at)\s+"
                r"(?P<website>.+)",
                routed,
            )
        if not website_match:
            website_match = re.fullmatch(
                r"(?:open|launch|start)\s+"
                r"(?:a |an |the )?"
                r"(?:new\s+)?"
                r"(?:(?:browser)\s+)?"
                r"(?:tab|window)\s+"
                r"(?:with|to|for|at)\s+"
                r"(?P<website>.+)",
                routed,
            )
        if website_match and (
            url := DeterministicRouter._website_url(website_match.group("website"))
        ):
            application = (website_match.groupdict().get("application") or "browser").strip()
            private = bool(website_match.groupdict().get("private_before")) or bool(
                (website_match.groupdict().get("private_after") or "").strip()
            )
            arguments: dict[str, object] = {
                "application": application,
                "url": url,
            }
            if private:
                arguments["private"] = True
            return Intent(
                name="windows.launcher",
                arguments=arguments,
                confidence=0.95,
                source="rule",
            )
        match = re.fullmatch(
            rf"(?:open|launch|start)\s+"
            rf"(?:a |an |the )?"
            rf"(?:new\s+)?"
            rf"(?P<private_before>private|incognito|inprivate)?\s*"
            rf"(?P<application>{browser_names})\s+"
            rf"(?:(?:browser)\s+)?"
            rf"(?:(?P<private_after>private|incognito|inprivate)\s+)?"
            rf"(?:tab|window)",
            routed,
        )
        if not match:
            match = re.fullmatch(
                rf"(?:open|launch|start)\s+"
                rf"(?:a |an |the )?"
                rf"(?:new\s+)?"
                rf"(?P<private_before>private|incognito|inprivate)\s+"
                rf"(?P<application>{browser_names})",
                routed,
            )
        if not match:
            match = re.fullmatch(
                rf"(?:open|launch|start)\s+"
                rf"(?:a |an |the )?"
                rf"(?:new\s+)?"
                rf"(?P<application>{browser_names})\s+"
                rf"(?P<private_after>private|incognito|inprivate)",
                routed,
            )
        if not match:
            match = re.fullmatch(
                rf"(?:open|launch|start)\s+"
                rf"(?:a |an |the )?"
                rf"(?:new\s+)?"
                rf"(?P<private_before>private|incognito|inprivate)\s+"
                rf"(?:(?:browser)\s+)?"
                rf"(?:tab|window)\s+"
                rf"(?:in|on|with|using)\s+"
                rf"(?P<application>{browser_names})",
                routed,
            )
        if not match:
            match = re.fullmatch(
                r"(?:open|launch|start)\s+"
                r"(?:a |an |the )?"
                r"(?:new\s+)?"
                r"(?P<private_before>private|incognito|inprivate)\s+"
                r"(?:(?:browser)\s+)?"
                r"(?:tab|window)",
                routed,
            )
        if not match:
            return None
        application = (match.groupdict().get("application") or "browser").strip()
        private = bool(match.groupdict().get("private_before")) or bool(
            (match.groupdict().get("private_after") or "").strip()
        )
        if application == "browser" and not private:
            operation = "new_window" if routed.endswith("window") else "new_tab"
            return Intent(
                name="windows.context_navigation",
                arguments={"target": "browser", "operation": operation},
                confidence=0.94,
                source="rule",
            )
        arguments: dict[str, object] = {
            "application": application,
        }
        if private:
            arguments["private"] = True
        return Intent(
            name="windows.launcher",
            arguments=arguments,
            confidence=0.94,
            source="rule",
        )

    @staticmethod
    def _route_website_first_private_browser_launch(
        routed: str, browser_names: str
    ) -> Intent | None:
        patterns = (
            rf"(?:open|launch|start)\s+"
            rf"(?P<website>.+?)\s+"
            rf"(?:with|in|on|using|for)\s+"
            rf"(?:a |an |the )?"
            rf"(?P<private>private|incognito|inprivate)\s+"
            rf"(?:(?:browser)\s+)?"
            rf"(?:tab|window)\s+"
            rf"(?:(?:open|opened)\s+)?"
            rf"(?:for|in|on|with|using)\s+"
            rf"(?P<application>{browser_names})",
            rf"(?:open|launch|start)\s+"
            rf"(?P<website>.+?)\s+"
            rf"(?:with|in|on|using|for)\s+"
            rf"(?:a |an |the )?"
            rf"(?P<private>private|incognito|inprivate)\s+"
            rf"(?P<application>{browser_names})\s+"
            rf"(?:(?:browser)\s+)?"
            rf"(?:tab|window)",
            rf"(?:open|launch|start)\s+"
            rf"(?P<website>.+?)\s+"
            rf"(?:with|in|on|using|for)\s+"
            rf"(?P<application>{browser_names})\s+"
            rf"(?P<private>private|incognito|inprivate)\s+"
            rf"(?:(?:browser)\s+)?"
            rf"(?:tab|window)",
        )
        for pattern in patterns:
            if not (match := re.fullmatch(pattern, routed)):
                continue
            if not (url := DeterministicRouter._website_url(match.group("website"))):
                continue
            return Intent(
                name="windows.launcher",
                arguments={
                    "application": match.group("application").strip(),
                    "private": True,
                    "url": url,
                },
                confidence=0.95,
                source="rule",
            )
        return None

    def _split_launch_target(self, target: str) -> tuple[str, str | None]:
        target = target.strip()
        if website_browser := self._split_website_browser_target(target):
            return website_browser
        target = self._strip_launch_placement_suffix(target)
        folded = target.casefold()
        for name in self._registered_application_names():
            if folded == name:
                return target[: len(name)].strip(), None
            if folded.startswith(f"{name} "):
                remainder = target[len(name) :].strip()
                if self._looks_like_follow_up(remainder):
                    return target[: len(name)].strip(), remainder
        for separator in (" and then ", " then ", " and "):
            if separator not in folded:
                continue
            index = folded.find(separator)
            application = target[:index].strip()
            remainder = target[index + len(separator) :].strip()
            if application and self._looks_like_follow_up(remainder):
                return application, remainder
        if " " in target:
            parts = target.split()
            for size in (2, 1):
                application = " ".join(parts[:size])
                remainder = " ".join(parts[size:])
                if (
                    application.casefold() in self.BROWSER_APPLICATIONS
                    and remainder
                    and self._looks_like_follow_up(remainder)
                ):
                    return application, remainder
        return target, None

    def _split_website_browser_target(self, target: str) -> tuple[str, str] | None:
        folded = target.casefold()
        names = self._registered_application_names() + tuple(
            sorted(self.BROWSER_APPLICATIONS, key=len, reverse=True)
        )
        for name in names:
            for preposition in (" on ", " in ", " with ", " using "):
                suffix = f"{preposition}{name}"
                if not folded.endswith(suffix):
                    continue
                website = target[: -len(suffix)].strip()
                if website and self._website_url(website):
                    return name, website
        return None

    @staticmethod
    def _file_destination_folder(target: str) -> str | None:
        target = re.sub(r"^(?:a|an|the|my)\s+", "", target.strip().casefold())
        assistant_folder_patterns = {
            "assistant folder",
            "assistant folder on desktop",
            "assistant folder on my desktop",
            "assistant folder on the desktop",
            "assistant folder in desktop",
            "assistant folder in my desktop",
            "assistant folder in the desktop",
        }
        if target in assistant_folder_patterns:
            return "${assistant_folder}"
        return target

    @staticmethod
    def _text_filename(raw_name: str | None) -> str:
        name = (raw_name or "my_text_file.txt").strip().strip(".")
        if not name.casefold().endswith(".txt"):
            name = f"{name}.txt"
        return re.sub(r'[<>:"/\\|?*]+', "_", name)

    @staticmethod
    def _monitor_index(text: str) -> int | None:
        cleaned = re.sub(
            r"\b(?:my|the|monitor|screen|display)\b",
            "",
            text.strip().casefold(),
        ).strip()
        ordinal_words = {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
            "fifth": 5,
            "sixth": 6,
            "seventh": 7,
            "eighth": 8,
            "ninth": 9,
            "tenth": 10,
        }
        if cleaned in ordinal_words:
            return ordinal_words[cleaned]
        if match := re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", cleaned):
            return int(match.group(1))
        try:
            value = spoken_number(cleaned)
        except ValueError:
            return None
        return int(value) if value >= 1 and value.is_integer() else None

    @staticmethod
    def _route_launch_placement(application: str, target: str) -> Intent | None:
        monitor = DeterministicRouter._placement_monitor(target)
        if monitor is None:
            return None
        return Intent(
            name="windows.application_window",
            arguments={
                "operation": "move_to_monitor",
                "process_name": executable_from_command_name(application),
                "monitor": monitor,
            },
            confidence=0.88,
            source="rule",
        )

    @staticmethod
    def _placement_monitor(target: str) -> int | None:
        if match := re.search(
            r"\s+on\s+(?:(?:my|the)\s+)?(?:(?:monitor|screen|display)\s+(.+?)|"
            r"(.+?)\s+(?:monitor|screen|display))$",
            target,
            flags=re.I,
        ):
            return DeterministicRouter._monitor_index(
                (match.group(1) or match.group(2) or "").strip()
            )
        return None

    @staticmethod
    def _strip_launch_placement_suffix(target: str) -> str:
        monitor = (
            r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
            r"one|two|three|four|five|six|seven|eight|nine|ten|"
            r"\d+(?:st|nd|rd|th)?)"
        )
        return re.sub(
            rf"\s+on\s+(?:(?:my|the)\s+)?(?:(?:monitor|screen|display)\s+{monitor}|"
            rf"{monitor}\s+(?:monitor|screen|display))$",
            "",
            target,
            flags=re.I,
        ).strip()

    @staticmethod
    def _looks_like_follow_up(text: str) -> bool:
        text = re.sub(r"^(?:and then|and|then)\s+", "", text.strip().casefold())
        return bool(DeterministicRouter._website_url(text)) or bool(
            re.match(
                r"(?:search|find|look up|google|bing|duckduckgo|open|go to|navigate to|"
                r"read|fetch|ask|type|click|focus|minimize|maximize|close)\b",
                text,
            )
        )

    @staticmethod
    def _is_direct_launcher_follow_up(intent: Intent, follow_up: Intent) -> bool:
        return (
            follow_up.name == "windows.launcher"
            and intent.arguments.get("application") == follow_up.arguments.get("application")
            and bool(follow_up.arguments.get("url"))
        )

    @staticmethod
    def _route_launch_follow_up(application: str, text: str | None) -> Intent | None:
        if not text:
            return None
        routed = command_text(text)
        routed = re.sub(r"^(?:and then|and|then)\s+", "", routed)
        if match := re.fullmatch(
            r"(?:search|find|look up|google|bing|duckduckgo)(?: (?:the )?web)?(?: for)? (.+)",
            routed,
        ):
            return Intent(
                name="windows.launcher",
                arguments={
                    "application": application,
                    "url": "https://search.brave.com/search?" + urlencode({"q": match.group(1)}),
                },
                confidence=0.94,
                source="rule",
            )
        if (
            match := re.fullmatch(
                r"(?:read|fetch|open|go to|navigate to) (.+)",
                routed,
            )
        ) and (url := DeterministicRouter._website_url(match.group(1))):
            return Intent(
                name="windows.launcher",
                arguments={"application": application, "url": url},
                confidence=0.94,
                source="rule",
            )
        if url := DeterministicRouter._website_url(routed):
            return Intent(
                name="windows.launcher",
                arguments={"application": application, "url": url},
                confidence=0.94,
                source="rule",
            )
        return None

    @staticmethod
    def _website_url(target: str) -> str | None:
        target = DeterministicRouter._normalize_website_target(target)
        if fallback_url := DeterministicRouter._website_fallback_url(target):
            return fallback_url
        if not target or re.search(r"\s", target):
            return None
        if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I):
            if not target.lower().startswith("https://"):
                return None
            parsed = urlsplit(target)
        else:
            parsed = urlsplit(f"https://{target}")
        if parsed.scheme != "https" or not parsed.netloc or "." not in parsed.netloc:
            return None
        hostname = parsed.hostname or ""
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", hostname, re.I):
            return None
        return urlunsplit(parsed)

    @staticmethod
    def _normalize_website_target(target: str) -> str:
        target = target.strip().rstrip(".,;")
        replacements = (
            (r"\s+(?:dot|period)\s+", "."),
            (r"\s+(?:forward\s+slash|slash)\s+", "/"),
            (r"\s+(?:question\s+mark)\s+", "?"),
            (r"\s+(?:ampersand|and)\s+", "&"),
            (r"\s+(?:equals|equal)\s+", "="),
        )
        for pattern, replacement in replacements:
            target = re.sub(pattern, replacement, target, flags=re.I)
        return target

    @staticmethod
    def _website_fallback_url(target: str) -> str | None:
        target = re.sub(
            r"^(?:a|an|the)\s+",
            "",
            target.strip().casefold(),
        )
        if target in DeterministicRouter.WEBSITE_APPLICATIONS:
            return DeterministicRouter.WEBSITE_APPLICATIONS[target]
        return DeterministicRouter._domain_url(target)

    @staticmethod
    def _domain_url(target: str) -> str | None:
        if not target or re.search(r"\s", target):
            return None
        if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I):
            return None
        parsed = urlsplit(f"https://{target}")
        hostname = parsed.hostname or ""
        if not parsed.netloc or "." not in parsed.netloc:
            return None
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", hostname, re.I):
            return None
        return urlunsplit(parsed)
