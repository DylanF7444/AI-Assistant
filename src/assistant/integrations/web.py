from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import certifi

USER_AGENT = "LocalAssistant/0.1"
DUCKDUCKGO_HTML_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.ignored:
            self.ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._field: str | None = None
        self._depth = 0
        self._parts: list[str] = []
        self._url = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._field is not None:
            self._depth += 1
            return
        attributes = dict(attrs)
        classes = set(str(attributes.get("class", "")).split())
        if tag == "a" and "result__a" in classes:
            url = self._result_url(str(attributes.get("href", "")))
            if url:
                self._begin("title", url)
        elif "result__snippet" in classes and self.results:
            self._begin("description", self.results[-1]["url"])

    def handle_endtag(self, _tag: str) -> None:
        if self._field is None:
            return
        self._depth -= 1
        if self._depth:
            return
        value = re.sub(r"\s+", " ", html.unescape("".join(self._parts))).strip()
        if self._field == "title" and value:
            self.results.append(
                {
                    "title": value,
                    "url": self._url,
                    "description": "",
                }
            )
        elif self._field == "description" and self.results:
            self.results[-1]["description"] = value
        self._field = None
        self._parts = []
        self._url = ""

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._parts.append(data)

    def _begin(self, field: str, url: str) -> None:
        self._field = field
        self._depth = 1
        self._parts = []
        self._url = url

    @staticmethod
    def _result_url(raw_url: str) -> str:
        if raw_url.startswith("//"):
            raw_url = f"https:{raw_url}"
        parsed = urlparse(raw_url)
        if parsed.hostname and parsed.hostname.casefold().endswith("duckduckgo.com"):
            redirected = parse_qs(parsed.query).get("uddg", ())
            if redirected:
                raw_url = redirected[0]
                parsed = urlparse(raw_url)
        if parsed.scheme != "https" or not parsed.hostname:
            return ""
        return raw_url


def _validate_public_https(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("Only public HTTPS URLs are allowed")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as error:
        raise ValueError(f"Could not resolve host: {parsed.hostname}") from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("Private, local, and reserved network targets are blocked")


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_secure_opener(context: ssl.SSLContext):
    return build_opener(
        _SafeRedirectHandler(),
        HTTPSHandler(context=context),
    )


def _is_certificate_verification_error(error: URLError) -> bool:
    reason = getattr(error, "reason", None)
    return (
        isinstance(reason, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(reason)
    ) or "CERTIFICATE_VERIFY_FAILED" in str(error)


def _open_request(request: Request, *, timeout: float, opener=None):
    if opener is not None:
        return opener.open(request, timeout=timeout)

    contexts = (
        ssl.create_default_context(),
        ssl.create_default_context(cafile=certifi.where()),
    )
    for index, context in enumerate(contexts):
        try:
            return _build_secure_opener(context).open(request, timeout=timeout)
        except URLError as error:
            if index == 0 and _is_certificate_verification_error(error):
                continue
            raise
    raise RuntimeError("No secure web opener was available")


def _request_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    timeout: float = 30,
    opener=None,
) -> dict[str, Any]:
    _validate_public_https(url)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with _open_request(request, timeout=timeout, opener=opener) as response:
            return json.loads(response.read(2_000_001).decode("utf-8"))
    except HTTPError as error:
        detail = error.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Network request failed: {error}") from error


class WebClient:
    def __init__(self, opener=None) -> None:
        self.opener = opener

    async def search(
        self,
        query: str,
        provider: str = "duckduckgo",
        api_key: str | None = None,
        count: int = 5,
    ) -> list[dict[str, str]]:
        if provider == "duckduckgo":
            return await self._search_duckduckgo(query, count)
        if provider == "brave":
            if not api_key:
                raise RuntimeError("A Brave Search API key is not configured")
            return await self._search_brave(query, api_key, count)
        raise ValueError(f"Unsupported search provider: {provider}")

    async def search_news(
        self,
        query: str,
        count: int = 5,
    ) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._search_google_news, query, count)

    def _search_google_news(
        self,
        query: str,
        count: int,
    ) -> list[dict[str, str]]:
        url = "https://news.google.com/rss/search?" + urlencode(
            {
                "q": query,
                "hl": "en-US",
                "gl": "US",
                "ceid": "US:en",
            }
        )
        _validate_public_https(url)
        request = Request(
            url,
            headers={
                "Accept": "application/rss+xml, application/xml",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with _open_request(request, timeout=30, opener=self.opener) as response:
                content_type = response.headers.get_content_type()
                if content_type not in {"application/rss+xml", "application/xml", "text/xml"}:
                    raise ValueError(f"Unsupported news content type: {content_type}")
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("News response exceeds the 1 MB download limit")
        except HTTPError as error:
            raise RuntimeError(f"HTTP {error.code} while searching news") from error
        except URLError as error:
            raise RuntimeError(f"News search failed: {error}") from error
        lowered_raw = raw.lower()
        if b"<!doctype" in lowered_raw or b"<!entity" in lowered_raw:
            raise RuntimeError("News search returned unsupported XML declarations")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as error:
            raise RuntimeError("News search returned invalid XML") from error
        results: list[dict[str, str]] = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            item_url = (item.findtext("link") or "").strip()
            source = (item.findtext("source") or "").strip()
            published = (item.findtext("pubDate") or "").strip()
            if not title or not item_url:
                continue
            details = " — ".join(value for value in (source, published) if value)
            results.append(
                {
                    "title": title,
                    "url": item_url,
                    "description": details,
                }
            )
            if len(results) >= count:
                break
        return results

    async def _search_duckduckgo(self, query: str, count: int) -> list[dict[str, str]]:
        url = "https://api.duckduckgo.com/?" + urlencode(
            {
                "q": query,
                "format": "json",
                "no_html": 1,
                "no_redirect": 1,
                "skip_disambig": 0,
            }
        )
        payload = await asyncio.to_thread(
            _request_json,
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            opener=self.opener,
        )
        results: list[dict[str, str]] = []
        abstract = str(payload.get("AbstractText", "")).strip()
        abstract_url = str(payload.get("AbstractURL", "")).strip()
        if abstract and abstract_url:
            results.append(
                {
                    "title": str(payload.get("Heading", "")).strip() or query,
                    "url": abstract_url,
                    "description": abstract,
                }
            )
        for item in payload.get("Results", []):
            self._append_duckduckgo_result(results, item)
        for item in payload.get("RelatedTopics", []):
            if isinstance(item, dict) and isinstance(item.get("Topics"), list):
                for topic in item["Topics"]:
                    self._append_duckduckgo_result(results, topic)
            else:
                self._append_duckduckgo_result(results, item)
        if results:
            return results[:count]
        return await asyncio.to_thread(self._search_duckduckgo_html, query, count)

    def _search_duckduckgo_html(
        self,
        query: str,
        count: int,
    ) -> list[dict[str, str]]:
        url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
        _validate_public_https(url)
        request = Request(
            url,
            headers={
                "Accept": "text/html",
                "User-Agent": DUCKDUCKGO_HTML_USER_AGENT,
            },
        )
        try:
            with _open_request(request, timeout=30, opener=self.opener) as response:
                content_type = response.headers.get_content_type()
                if content_type != "text/html":
                    raise ValueError(f"Unsupported search content type: {content_type}")
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("Search response exceeds the 1 MB download limit")
                charset = response.headers.get_content_charset() or "utf-8"
                page = raw.decode(charset, errors="replace")
        except HTTPError as error:
            raise RuntimeError(f"HTTP {error.code} while searching the web") from error
        except URLError as error:
            raise RuntimeError(f"Web search failed: {error}") from error
        parser = _DuckDuckGoHTMLParser()
        parser.feed(page)
        return parser.results[:count]

    @staticmethod
    def _append_duckduckgo_result(results: list[dict[str, str]], item: Any) -> None:
        if not isinstance(item, dict):
            return
        url = str(item.get("FirstURL", "")).strip()
        text = html.unescape(str(item.get("Text", "")).strip())
        if not url or not text:
            return
        title, separator, description = text.partition(" - ")
        results.append(
            {
                "title": title.strip(),
                "url": url,
                "description": description.strip() if separator else text,
            }
        )

    async def _search_brave(self, query: str, api_key: str, count: int) -> list[dict[str, str]]:
        url = "https://api.search.brave.com/res/v1/web/search?" + urlencode(
            {"q": query, "count": count, "safesearch": "moderate"}
        )
        payload = await asyncio.to_thread(
            _request_json,
            url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
                "User-Agent": USER_AGENT,
            },
            opener=self.opener,
        )
        return [
            {
                "title": str(item.get("title", "")),
                "url": str(item.get("url", "")),
                "description": html.unescape(str(item.get("description", ""))),
            }
            for item in payload.get("web", {}).get("results", [])[:count]
        ]

    async def fetch(self, url: str, max_characters: int = 12000) -> dict[str, str]:
        return await asyncio.to_thread(self._fetch_sync, url, max_characters)

    def _fetch_sync(self, url: str, max_characters: int) -> dict[str, str]:
        _validate_public_https(url)
        request = Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"},
        )
        try:
            with _open_request(request, timeout=30, opener=self.opener) as response:
                content_type = response.headers.get_content_type()
                if content_type not in {"text/html", "text/plain"}:
                    raise ValueError(f"Unsupported content type: {content_type}")
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("Page exceeds the 1 MB download limit")
                charset = response.headers.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
        except HTTPError as error:
            raise RuntimeError(f"HTTP {error.code} while fetching the page") from error
        except URLError as error:
            raise RuntimeError(f"Page fetch failed: {error}") from error
        if content_type == "text/html":
            parser = _TextExtractor()
            parser.feed(text)
            text = " ".join(parser.parts)
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        return {"url": url, "text": text[:max_characters]}


class CloudModelClient:
    def __init__(self, opener=None) -> None:
        self.opener = opener

    async def ask(
        self,
        provider: str,
        api_key: str,
        model: str,
        question: str,
        max_tokens: int,
    ) -> str:
        if provider == "openai":
            payload = await asyncio.to_thread(
                _request_json,
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                payload={
                    "model": model,
                    "input": question,
                    "max_output_tokens": max_tokens,
                },
                opener=self.opener,
            )
            text = "\n".join(
                str(content.get("text", ""))
                for item in payload.get("output", [])
                if item.get("type") == "message"
                for content in item.get("content", [])
                if content.get("type") == "output_text"
            ).strip()
        elif provider == "anthropic":
            payload = await asyncio.to_thread(
                _request_json,
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                payload={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": question}],
                },
                opener=self.opener,
            )
            text = "\n".join(
                str(item.get("text", ""))
                for item in payload.get("content", [])
                if item.get("type") == "text"
            ).strip()
        else:
            raise ValueError(f"Unsupported cloud provider: {provider}")
        if not text:
            raise RuntimeError("The cloud model returned no text")
        return text


class CodexExecClient:
    def __init__(
        self,
        executable: str | Path = "codex",
        *,
        cwd: str | Path | None = None,
        timeout_seconds: float = 120,
        extra_args: tuple[str, ...] = (
            "-c",
            'approval_policy="never"',
            "--sandbox",
            "read-only",
        ),
    ) -> None:
        self.executable = str(executable)
        self.cwd = Path(cwd) if cwd is not None else None
        self.timeout_seconds = timeout_seconds
        self.extra_args = extra_args

    async def ask(
        self,
        question: str,
        *,
        model: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._ask_sync,
            question,
            model=model,
            cancel_event=cancel_event,
        )

    def _ask_sync(
        self,
        question: str,
        *,
        model: str | None,
        cancel_event: asyncio.Event | None,
    ) -> str:
        executable = self._resolve_executable()
        command = [executable, "exec", *self.extra_args]
        if model:
            command.extend(["--model", model])
        command.append(question)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.cwd) if self.cwd is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )
        except OSError as error:
            raise RuntimeError(f"Could not start codex exec: {error}") from error
        stdout = stderr = ""
        try:
            deadline = time.monotonic() + self.timeout_seconds
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    raise RuntimeError("codex exec exceeded its timeout")
                if cancel_event is not None and cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise RuntimeError("codex exec was cancelled")
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:
                stdout, stderr = process.communicate(timeout=0)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.communicate()
            raise RuntimeError("codex exec exceeded its timeout") from error
        if process.returncode != 0:
            detail = (stderr or stdout).strip()
            raise RuntimeError(
                f"codex exec failed with exit code {process.returncode}"
                + (f": {detail}" if detail else "")
            )
        text = stdout.strip() or stderr.strip()
        if not text:
            raise RuntimeError("codex exec returned no text")
        return text

    def _resolve_executable(self) -> str:
        if self.executable.casefold() != "codex":
            return shutil.which(self.executable) or self.executable
        bundled = self._latest_user_codex_binary()
        if bundled is not None:
            return str(bundled)
        return shutil.which(self.executable) or self.executable

    @staticmethod
    def _latest_user_codex_binary() -> Path | None:
        local_app_data = os.getenv("LOCALAPPDATA")
        if not local_app_data:
            return None
        root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        if not root.exists():
            return None
        candidates = [path for path in root.glob("*/codex.exe") if path.is_file()]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)
