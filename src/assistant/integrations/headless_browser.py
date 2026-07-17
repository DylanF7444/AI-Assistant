from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlparse

from assistant.integrations.web import USER_AGENT, _validate_public_https
from assistant.policy.network import OfflineNetworkError


@dataclass(frozen=True)
class RenderedPage:
    url: str
    title: str
    text: str
    links: tuple[dict[str, str], ...] = ()


class HeadlessBrowser(Protocol):
    async def read_page(
        self,
        url: str,
        *,
        max_characters: int,
        max_links: int,
        timeout_seconds: float,
        wait_after_load_ms: int,
    ) -> RenderedPage: ...


class PlaywrightHeadlessBrowser:
    """Read a rendered page in a fresh, non-persistent browser context."""

    def __init__(self, *, offline_mode: bool = False) -> None:
        self.offline_mode = offline_mode

    async def read_page(
        self,
        url: str,
        *,
        max_characters: int = 12_000,
        max_links: int = 30,
        timeout_seconds: float = 30,
        wait_after_load_ms: int = 250,
    ) -> RenderedPage:
        if self.offline_mode:
            raise OfflineNetworkError("Headless browsing is blocked by offline mode")
        await asyncio.to_thread(_validate_public_https, url)
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError(
                "Headless browsing is not installed. Install .[browser], then run "
                "`python -m playwright install chromium`."
            ) from exc

        timeout_ms = round(timeout_seconds * 1000)
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover - depends on local browser install
                raise RuntimeError(
                    "The Playwright Chromium runtime is unavailable. Run "
                    "`python -m playwright install chromium`."
                ) from exc
            try:
                context = await browser.new_context(
                    accept_downloads=False,
                    ignore_https_errors=False,
                    java_script_enabled=True,
                    service_workers="block",
                    user_agent=USER_AGENT,
                )
                try:
                    await context.route("**/*", self._route_public_request)
                    await context.route_web_socket("**/*", self._block_websocket)
                    page = await context.new_page()
                    page.set_default_timeout(timeout_ms)
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    if response is not None and response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status} while loading the page")
                    if wait_after_load_ms:
                        await page.wait_for_timeout(wait_after_load_ms)
                    final_url = page.url
                    await asyncio.to_thread(_validate_public_https, final_url)
                    title = (await page.title()).strip()
                    text = " ".join((await page.locator("body").inner_text()).split())
                    raw_links = (
                        await page.locator("a[href]").evaluate_all(
                            """
                            (elements, limit) => elements.slice(0, limit).map((element) => ({
                              text: (element.innerText || element.textContent || '').trim(),
                              href: element.href || ''
                            }))
                            """,
                            max_links,
                        )
                        if max_links
                        else []
                    )
                    links = self._clean_links(final_url, raw_links, max_links)
                    return RenderedPage(
                        url=final_url,
                        title=title,
                        text=text[:max_characters],
                        links=links,
                    )
                finally:
                    await context.close()
            finally:
                await browser.close()

    @staticmethod
    async def _route_public_request(route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        if parsed.scheme in {"data", "blob", "about"}:
            await route.continue_()
            return
        if request.resource_type in {"font", "image", "media"}:
            await route.abort()
            return
        try:
            await asyncio.to_thread(_validate_public_https, request.url)
        except ValueError:
            await route.abort()
            return
        await route.continue_()

    @staticmethod
    async def _block_websocket(websocket_route) -> None:
        await websocket_route.close(code=1008, reason="Headless read blocks WebSockets")

    @staticmethod
    def _clean_links(
        base_url: str,
        values: list[dict],
        limit: int,
    ) -> tuple[dict[str, str], ...]:
        links = []
        seen = set()
        for value in values:
            href = urljoin(base_url, str(value.get("href", "")).strip())
            parsed = urlparse(href)
            if parsed.scheme != "https" or not parsed.hostname or href in seen:
                continue
            seen.add(href)
            links.append(
                {
                    "text": " ".join(str(value.get("text", "")).split())[:200],
                    "url": href,
                }
            )
            if len(links) >= limit:
                break
        return tuple(links)
