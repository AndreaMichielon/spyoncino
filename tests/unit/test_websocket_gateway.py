import asyncio

import httpx
import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import HealthStatus, HealthSummary, ModuleConfig
from spyoncino.modules.dashboard.websocket_gateway import WebsocketGateway


@pytest.mark.asyncio
async def test_websocket_gateway_buffers_events() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    module = WebsocketGateway()
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "serve_http": False,
                "topics": ["status.health.summary"],
                "buffer_size": 10,
            }
        )
    )
    await module.start()

    payload = HealthSummary(status="healthy", modules={"test": HealthStatus(status="healthy")})
    ready = asyncio.Event()

    async def wait_for_buffer(topic: str, _payload: HealthSummary) -> None:
        ready.set()

    bus.subscribe("status.health.summary", wait_for_buffer)

    await bus.publish("status.health.summary", payload)
    await asyncio.wait_for(ready.wait(), timeout=1.0)

    async with httpx.AsyncClient(app=module.app, base_url="http://test") as client:
        response = await client.get("/events", params={"limit": 1})
        assert response.status_code == 200
        data = response.json()
        assert data["events"]
        assert data["events"][0]["topic"] == "status.health.summary"

    await module.stop()
    await bus.stop()
