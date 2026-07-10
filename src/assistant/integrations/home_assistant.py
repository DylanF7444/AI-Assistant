from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import urllib.request
from typing import Any

_IDENTIFIER = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_SERVICE = re.compile(r"^[a-z0-9_]+$")


class HomeAssistantAdapter:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 10,
        opener=urllib.request.urlopen,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Invalid Home Assistant URL")
        if parsed.scheme != "https" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("Remote Home Assistant connections require HTTPS")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with self.opener(request, timeout=self.timeout_seconds) as response:
            return json.load(response)

    async def health(self) -> dict[str, Any]:
        try:
            value = await asyncio.to_thread(self._request, "GET", "/api/")
            return {"healthy": True, "message": value.get("message")}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}

    async def state(self, entity_id: str) -> dict[str, Any]:
        if not _IDENTIFIER.fullmatch(entity_id):
            raise ValueError("Invalid Home Assistant entity ID")
        return await asyncio.to_thread(
            self._request,
            "GET",
            f"/api/states/{urllib.parse.quote(entity_id, safe='.')}",
        )

    async def call_service(
        self,
        domain: str,
        service: str,
        *,
        entity_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        if not _SERVICE.fullmatch(domain) or not _SERVICE.fullmatch(service):
            raise ValueError("Invalid Home Assistant service")
        if entity_id and not _IDENTIFIER.fullmatch(entity_id):
            raise ValueError("Invalid Home Assistant entity ID")
        payload = dict(data or {})
        if entity_id:
            payload["entity_id"] = entity_id
        return await asyncio.to_thread(
            self._request, "POST", f"/api/services/{domain}/{service}", payload
        )
