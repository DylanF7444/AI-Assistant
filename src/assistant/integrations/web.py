from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


USER_AGENT = "LocalAssistant/0.1"


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
        with (opener or build_opener(_SafeRedirectHandler())).open(
            request, timeout=timeout
        ) as response:
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

    async def _search_duckduckgo(
        self, query: str, count: int
    ) -> list[dict[str, str]]:
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
        return results[:count]

    @staticmethod
    def _append_duckduckgo_result(
        results: list[dict[str, str]], item: Any
    ) -> None:
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

    async def _search_brave(
        self, query: str, api_key: str, count: int
    ) -> list[dict[str, str]]:
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
        return await asyncio.to_thread(
            self._fetch_sync, url, max_characters
        )

    def _fetch_sync(self, url: str, max_characters: int) -> dict[str, str]:
        _validate_public_https(url)
        request = Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"},
        )
        try:
            with (self.opener or build_opener(_SafeRedirectHandler())).open(
                request, timeout=30
            ) as response:
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
