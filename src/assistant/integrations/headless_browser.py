from __future__ import annotations

import asyncio
import re
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
    items: tuple[dict[str, str], ...] = ()


class HeadlessBrowser(Protocol):
    async def read_page(
        self,
        url: str,
        *,
        max_characters: int,
        max_links: int,
        timeout_seconds: float,
        wait_after_load_ms: int,
        extract: str | None,
        max_items: int,
    ) -> RenderedPage: ...


class PlaywrightHeadlessBrowser:
    """Read a rendered page in a fresh, non-persistent browser context."""

    _ITEM_SELECTOR = ", ".join(
        (
            "article",
            "[itemtype*='Product' i]",
            "[itemtype*='Offer' i]",
            "[class*='card' i]",
            "[class*='listing' i]",
            "[class*='product' i]",
            "[class*='result' i]",
            "[data-testid*='deal' i]",
            "[data-testid*='card' i]",
            "[data-testid*='product' i]",
            "[data-testid*='result' i]",
        )
    )
    _ITEM_EXTRACTION_SCRIPT = r"""
        (elements, limit) => elements.slice(0, limit).flatMap((element, order) => {
          const style = window.getComputedStyle(element);
          const box = element.getBoundingClientRect();
          if (style.display === 'none' || style.visibility === 'hidden' ||
              box.width === 0 || box.height === 0) return [];
          const text = (element.innerText || element.textContent || '')
            .replace(/\s+/g, ' ').trim();
          if (text.length < 8) return [];
          const anchor = element.matches('a[href]')
            ? element
            : element.querySelector('a[href]');
          if (!anchor || !anchor.href) return [];
          const titleElement = element.querySelector(
            "h1, h2, h3, h4, [class*='title' i], [data-testid*='title' i]"
          );
          const title = ((titleElement &&
            (titleElement.innerText || titleElement.textContent)) ||
            anchor.innerText || anchor.textContent || '')
            .replace(/\s+/g, ' ').trim();
          const identity = [
            element.tagName,
            element.id,
            typeof element.className === 'string' ? element.className : '',
            element.getAttribute('data-testid') || '',
            element.getAttribute('itemtype') || '',
            element.getAttribute('role') || ''
          ].join(' ');
          return [{
            order,
            identity,
            title: title.slice(0, 300),
            text: text.slice(0, 1600),
            href: anchor.href
          }];
        })
    """

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
        extract: str | None = None,
        max_items: int = 5,
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
            browser = await self._launch_browser(playwright)
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
                    raw_items = (
                        await page.locator(self._ITEM_SELECTOR).evaluate_all(
                            self._ITEM_EXTRACTION_SCRIPT,
                            min(500, max(100, max_items * 50)),
                        )
                        if extract and max_items
                        else []
                    )
                    items = self._clean_items(final_url, raw_items, extract or "", max_items)
                    return RenderedPage(
                        url=final_url,
                        title=title,
                        text=text[:max_characters],
                        links=links,
                        items=items,
                    )
                finally:
                    await context.close()
            finally:
                await browser.close()

    @staticmethod
    async def _launch_browser(playwright):
        attempts = (
            ("Microsoft Edge", {"channel": "msedge"}),
            ("Google Chrome", {"channel": "chrome"}),
            ("Playwright Chromium", {}),
        )
        failures = []
        for label, options in attempts:
            try:
                return await playwright.chromium.launch(headless=True, **options)
            except Exception as exc:  # pragma: no cover - depends on installed browsers
                failures.append(f"{label}: {exc}")
        detail = failures[-1] if failures else "no browser launch was attempted"
        raise RuntimeError(
            "No supported headless browser runtime is available. Install Microsoft Edge "
            "or Google Chrome, or run `python -m playwright install chromium`. "
            f"Last launch error: {detail}"
        )

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
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
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

    @staticmethod
    def _clean_items(
        base_url: str,
        values: list[dict],
        extract: str,
        limit: int,
    ) -> tuple[dict[str, str], ...]:
        if not extract or limit <= 0:
            return ()
        ignored = {
            "a",
            "an",
            "and",
            "first",
            "from",
            "give",
            "get",
            "latest",
            "me",
            "newest",
            "of",
            "on",
            "one",
            "show",
            "the",
            "top",
        }
        query_words = {
            word[:-1] if word.endswith("s") and len(word) > 3 else word
            for word in re.findall(r"[a-z0-9]+", extract.casefold())
            if word not in ignored
        }
        scored_by_url: dict[str, tuple[int, int, dict[str, str]]] = {}
        for fallback_order, value in enumerate(values):
            if not isinstance(value, dict):
                continue
            href = urljoin(base_url, str(value.get("href", "")).strip())
            parsed = urlparse(href)
            if parsed.scheme != "https" or not parsed.hostname:
                continue
            title = " ".join(str(value.get("title", "")).split())[:300]
            text = " ".join(str(value.get("text", "")).split())[:1600]
            identity = " ".join(str(value.get("identity", "")).casefold().split())
            if not title and not text:
                continue
            folded = f"{identity} {title.casefold()} {text.casefold()}"
            normalized_words = {
                word[:-1] if word.endswith("s") and len(word) > 3 else word
                for word in re.findall(r"[a-z0-9]+", folded)
            }
            score = 0
            for word in query_words:
                if word in normalized_words:
                    score += 5
                if word in identity.replace("_", "").replace("-", ""):
                    score += 7
            if re.search(r"deal\s*card|product\s*card|offer\s*card", identity):
                score += 10
            score += 3 * sum(
                signal in identity
                for signal in ("deal", "card", "listing", "offer", "product", "result")
            )
            if re.search(r"(?:^|\s)(?:from\s+)?\$\d|\b\d{1,3}%\s+off\b", text, re.I):
                score += 5
            if title:
                score += 2
            if 20 <= len(text) <= 900:
                score += 2
            if any(
                signal in identity for signal in ("breadcrumb", "footer", "header", "menu", "nav")
            ):
                score -= 20
            if (
                query_words
                and not (query_words & normalized_words)
                and not any(
                    word in identity.replace("_", "").replace("-", "") for word in query_words
                )
            ):
                continue
            try:
                order = int(value.get("order", fallback_order))
            except (TypeError, ValueError):
                order = fallback_order
            item = {"title": title or text[:200], "text": text, "url": href}
            previous = scored_by_url.get(href)
            if previous is None or score > previous[0]:
                scored_by_url[href] = (score, order, item)
        ranked = sorted(scored_by_url.values(), key=lambda item: (-item[0], item[1]))
        return tuple(item for _score, _order, item in ranked[:limit])
