import asyncio

import httpx
import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import ConfigUpdate, ControlCommand, ModuleConfig
from spyoncino.modules.dashboard.control_api import ControlApi


@pytest.mark.asyncio
async def test_control_api_publishes_bus_events() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    module = ControlApi()
    module.set_bus(bus)
    await module.configure(ModuleConfig(options={"serve_api": False}))

    commands: list[ControlCommand] = []
    config_updates: list[ConfigUpdate] = []
    command_event = asyncio.Event()
    config_event = asyncio.Event()

    async def command_handler(topic: str, payload: ControlCommand) -> None:
        commands.append(payload)
        command_event.set()

    async def config_handler(topic: str, payload: ConfigUpdate) -> None:
        config_updates.append(payload)
        config_event.set()

    bus.subscribe("dashboard.control.command", command_handler)
    bus.subscribe("config.update", config_handler)

    await module.start()

    async with httpx.AsyncClient(app=module.app, base_url="http://test") as client:
        response = await client.post("/cameras/lab/state", json={"enabled": True})
        assert response.status_code == 202
        await asyncio.wait_for(command_event.wait(), timeout=0.2)

        response = await client.post(
            "/config/zones",
            json={
                "camera_id": "lab",
                "zones": [
                    {
                        "zone_id": "door",
                        "bounds": [0.0, 0.0, 1.0, 1.0],
                    }
                ],
            },
        )
        assert response.status_code == 202
        await asyncio.wait_for(config_event.wait(), timeout=0.2)

    await module.stop()
    await bus.stop()

    assert commands and commands[0].arguments["enabled"] is True
    assert config_updates
    zones = config_updates[0].changes["zoning"]["zones"]
    assert zones[0]["camera_id"] == "lab"
