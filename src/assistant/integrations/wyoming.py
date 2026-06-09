from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from typing import Any


class WyomingAdapter:
    """Minimal Wyoming JSONL + payload client with optional mutual TLS."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        if ssl_context is None and host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote Wyoming connections require TLS")
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.timeout_seconds = timeout_seconds

    async def exchange(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        payload: bytes = b"",
    ) -> tuple[dict[str, Any], bytes]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.host, self.port, ssl=self.ssl_context
            ),
            timeout=self.timeout_seconds,
        )
        header = {
            "type": event_type,
            "data": data or {},
            "data_length": 0,
            "payload_length": len(payload),
        }
        writer.write(json.dumps(header, separators=(",", ":")).encode() + b"\n")
        if payload:
            writer.write(payload)
        await writer.drain()
        try:
            line = await asyncio.wait_for(
                reader.readline(), timeout=self.timeout_seconds
            )
            response = json.loads(line)
            data_length = int(response.get("data_length", 0))
            payload_length = int(response.get("payload_length", 0))
            if data_length:
                response["data"] = json.loads(
                    await reader.readexactly(data_length)
                )
            response_payload = (
                await reader.readexactly(payload_length) if payload_length else b""
            )
            return response, response_payload
        finally:
            writer.close()
            await writer.wait_closed()

    async def describe(self) -> dict[str, Any]:
        response, _ = await self.exchange("describe")
        if response.get("type") != "info":
            raise RuntimeError("Wyoming endpoint did not return service info")
        return response

