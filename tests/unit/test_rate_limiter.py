import asyncio

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import ModuleConfig, SnapshotArtifact
from spyoncino.modules.output.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_throttles_events() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    limiter = RateLimiter(clock=lambda: 0.0)
    limiter.set_bus(bus)
    await limiter.configure(
        ModuleConfig(
            options={
                "input_topic": "event.snapshot.ready",
                "output_topic": "event.snapshot.allowed",
                "max_events": 1,
                "per_seconds": 60,
            }
        )
    )
    await limiter.start()

    received: list[SnapshotArtifact] = []
    signal = asyncio.Event()

    async def handler(topic: str, payload: SnapshotArtifact) -> None:
        received.append(payload)
        signal.set()

    bus.subscribe("event.snapshot.allowed", handler)

    artifact = SnapshotArtifact(camera_id="cam", artifact_path="path")
    await bus.publish("event.snapshot.ready", artifact)
    await asyncio.wait_for(signal.wait(), timeout=0.2)

    await bus.publish("event.snapshot.ready", artifact)
    await asyncio.sleep(0.05)

    await limiter.stop()
    await bus.stop()

    assert len(received) == 1
