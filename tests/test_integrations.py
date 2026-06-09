from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from assistant.integrations.devices import DeviceCommand, DeviceResult
from assistant.integrations.home_assistant import HomeAssistantAdapter
from assistant.integrations.mqtt import MQTTDeviceTransport
from assistant.integrations.wyoming import WyomingAdapter


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


@pytest.mark.asyncio
async def test_home_assistant_validates_targets_and_uses_bearer_auth():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse(b'{"state":"on"}')

    adapter = HomeAssistantAdapter(
        "https://home.local", "secret-token", opener=opener
    )
    state = await adapter.state("light.kitchen")
    assert state["state"] == "on"
    assert requests[0].headers["Authorization"] == "Bearer secret-token"
    with pytest.raises(ValueError):
        await adapter.state("../../secrets")
    with pytest.raises(ValueError):
        HomeAssistantAdapter("http://192.168.1.2:8123", "token")


@pytest.mark.asyncio
async def test_wyoming_local_jsonl_exchange():
    async def handler(reader, writer):
        request = json.loads(await reader.readline())
        assert request["type"] == "describe"
        writer.write(
            b'{"type":"info","data":{"services":["stt"]},"data_length":0,"payload_length":0}\n'
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        adapter = WyomingAdapter("127.0.0.1", port)
        response = await adapter.describe()
        assert response["data"]["services"] == ["stt"]
    finally:
        server.close()
        await server.wait_closed()


class FakeMQTTClient:
    def __init__(self):
        self.on_connect = None
        self.on_message = None
        self.tls = None

    def tls_set(self, **kwargs):
        self.tls = kwargs

    def connect(self, host, port, keepalive=20):
        self.on_connect(self, None, None, 0)

    def subscribe(self, topic, qos):
        self.response_topic = topic

    def publish(self, topic, payload, qos, retain):
        command = json.loads(payload)["command"]
        result = DeviceResult(
            command_id=command["command_id"],
            success=True,
            summary="done",
        )
        self.on_message(
            self, None, SimpleNamespace(payload=result.model_dump_json().encode())
        )

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


@pytest.mark.asyncio
async def test_mqtt_transport_uses_mutual_tls_and_signed_command(tmp_path: Path):
    files = []
    for name in ("ca.pem", "client.pem", "client.key"):
        path = tmp_path / name
        path.write_text("test")
        files.append(path)
    client = FakeMQTTClient()
    device_id = uuid4()
    transport = MQTTDeviceTransport(
        "broker.local",
        8883,
        ca_certificate=files[0],
        client_certificate=files[1],
        client_key=files[2],
        device_keys={device_id: b"shared-key"},
        client_factory=lambda: client,
    )
    result = await transport.send(
        DeviceCommand(device_id=device_id, capability="light.on")
    )
    assert result.success
    assert client.tls["cert_reqs"] is not None
