import asyncio

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import BusStatus, Frame


@pytest.mark.asyncio
async def test_publish_and_subscribe_round_trip() -> None:
    bus = EventBus(queue_size=8)
    await bus.start()

    received = asyncio.Event()
    payloads: list[Frame] = []

    async def handler(topic: str, payload: Frame) -> None:
        payloads.append(payload)
        received.set()

    bus.subscribe("camera.test.frame", handler)

    await bus.publish("camera.test.frame", Frame(camera_id="test"))
    await asyncio.wait_for(received.wait(), timeout=0.2)

    await bus.stop()

    assert payloads and payloads[0].camera_id == "test"


@pytest.mark.asyncio
async def test_bus_emits_status_telemetry() -> None:
    bus = EventBus(queue_size=4, telemetry_interval=0.01)
    await bus.start()

    statuses: list[BusStatus] = []
    received = asyncio.Event()

    async def handler(topic: str, payload: BusStatus) -> None:
        statuses.append(payload)
        received.set()

    bus.subscribe("status.bus", handler)
    await asyncio.wait_for(received.wait(), timeout=0.5)
    await bus.stop()

    assert statuses
    assert statuses[0].queue_capacity == 4
