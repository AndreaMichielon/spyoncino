import asyncio

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import DetectionEvent, ModuleConfig
from spyoncino.modules.event.deduplicator import EventDeduplicator


@pytest.mark.asyncio
async def test_deduplicator_drops_duplicates() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    dedupe = EventDeduplicator(clock=lambda: 0.0)
    dedupe.set_bus(bus)
    await dedupe.configure(
        ModuleConfig(
            options={
                "input_topic": "process.motion.detected",
                "output_topic": "process.motion.unique",
                "window_seconds": 5.0,
            }
        )
    )
    await dedupe.start()

    events: list[DetectionEvent] = []
    signal = asyncio.Event()

    async def handler(topic: str, payload: DetectionEvent) -> None:
        events.append(payload)
        signal.set()

    bus.subscribe("process.motion.unique", handler)

    detection = DetectionEvent(camera_id="cam", detector_id="motion", frame_ref="1")
    await bus.publish("process.motion.detected", detection)
    await asyncio.wait_for(signal.wait(), timeout=0.2)

    await bus.publish("process.motion.detected", detection)
    await asyncio.sleep(0.05)

    await dedupe.stop()
    await bus.stop()

    assert len(events) == 1
