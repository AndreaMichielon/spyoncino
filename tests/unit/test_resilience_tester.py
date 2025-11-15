import asyncio

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import Frame, ModuleConfig, ResilienceEvent
from spyoncino.modules.status.resilience_tester import ResilienceTester


@pytest.mark.asyncio
async def test_resilience_tester_drops_events() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    module = ResilienceTester()
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "enabled": True,
                "status_topic": "status.resilience.event",
                "scenarios": [
                    {
                        "name": "drop-all",
                        "topic": "camera.lab.frame",
                        "drop_probability": 1.0,
                    }
                ],
            }
        )
    )

    received = asyncio.Event()

    async def handle_frame(topic: str, payload: Frame) -> None:
        received.set()

    bus.subscribe("camera.lab.frame", handle_frame)

    events: list[ResilienceEvent] = []
    event_signal = asyncio.Event()

    async def handle_resilience(topic: str, payload: ResilienceEvent) -> None:
        events.append(payload)
        event_signal.set()

    bus.subscribe("status.resilience.event", handle_resilience)

    await module.start()
    await bus.publish("camera.lab.frame", Frame(camera_id="lab"))
    await asyncio.wait_for(event_signal.wait(), timeout=1.0)
    await module.stop()
    await bus.stop()

    assert not received.is_set()
    assert events and events[-1].action in {"drop", "status"}
