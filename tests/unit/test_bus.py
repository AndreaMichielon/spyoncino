import asyncio

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import Frame


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
