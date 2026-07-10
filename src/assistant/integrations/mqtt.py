from __future__ import annotations

import asyncio
import json
import ssl
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

from assistant.integrations.devices import (
    DeviceCommand,
    DeviceHealth,
    DeviceResult,
    sign_command,
)


class MQTTDeviceTransport:
    """Mutually authenticated local MQTT transport with command expiry."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ca_certificate: Path,
        client_certificate: Path,
        client_key: Path,
        device_keys: dict[UUID, bytes],
        topic_prefix: str = "local-assistant/v1",
        timeout_seconds: float = 10,
        client_factory=None,
    ) -> None:
        for path in (ca_certificate, client_certificate, client_key):
            if not path.is_file():
                raise FileNotFoundError(path)
        self.host = host
        self.port = port
        self.ca_certificate = ca_certificate
        self.client_certificate = client_certificate
        self.client_key = client_key
        self.device_keys = device_keys
        self.topic_prefix = topic_prefix.strip("/")
        self.timeout_seconds = timeout_seconds
        self.client_factory = client_factory

    def _client(self):
        if self.client_factory:
            client = self.client_factory()
        else:
            import paho.mqtt.client as mqtt

            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        client.tls_set(
            ca_certs=str(self.ca_certificate),
            certfile=str(self.client_certificate),
            keyfile=str(self.client_key),
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        return client

    async def send(self, command: DeviceCommand) -> DeviceResult:
        key = self.device_keys.get(command.device_id)
        if key is None:
            raise PermissionError("No authenticated key for this device")

        def exchange() -> DeviceResult:
            client = self._client()
            response_topic = f"{self.topic_prefix}/{command.device_id}/results/{command.command_id}"
            command_topic = f"{self.topic_prefix}/{command.device_id}/commands"
            completed = threading.Event()
            result: dict[str, Any] = {}

            def on_connect(active_client, _userdata, _flags, reason_code, _properties=None):
                if int(reason_code) != 0:
                    result["error"] = f"MQTT connection failed: {reason_code}"
                    completed.set()
                    return
                active_client.subscribe(response_topic, qos=1)
                payload = {
                    "command": command.model_dump(mode="json"),
                    "signature": sign_command(command, key),
                }
                active_client.publish(
                    command_topic,
                    json.dumps(payload, separators=(",", ":")),
                    qos=1,
                    retain=False,
                )

            def on_message(_client, _userdata, message):
                try:
                    result["value"] = DeviceResult.model_validate_json(message.payload)
                except Exception as exc:
                    result["error"] = str(exc)
                completed.set()

            client.on_connect = on_connect
            client.on_message = on_message
            client.connect(self.host, self.port, keepalive=20)
            client.loop_start()
            try:
                if not completed.wait(self.timeout_seconds):
                    raise TimeoutError("Device did not return a result")
                if "error" in result:
                    raise RuntimeError(result["error"])
                return result["value"]
            finally:
                client.disconnect()
                client.loop_stop()

        return await asyncio.to_thread(exchange)

    async def health(self, device_id: UUID) -> DeviceHealth:
        try:
            client = self._client()
            await asyncio.to_thread(client.connect, self.host, self.port, 5)
            await asyncio.to_thread(client.disconnect)
            return DeviceHealth(device_id=device_id, online=True)
        except Exception as exc:
            return DeviceHealth(
                device_id=device_id,
                online=False,
                details={"error": str(exc)},
            )
