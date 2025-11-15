import asyncio

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import DetectionEvent, ModuleConfig
from spyoncino.modules.process.detection_event_router import DetectionEventRouter


@pytest.mark.asyncio
async def test_detection_router_enforces_cooldown() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    fake_time = [0.0]

    module = DetectionEventRouter(clock=lambda: fake_time[0])
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "input_topic": "process.yolo.detected",
                "output_topic": "process.alert.detected",
                "cooldown_seconds": 5.0,
                "bbox_iou_threshold": 0.5,
            }
        )
    )

    detections: list[DetectionEvent] = []
    event = asyncio.Event()

    async def handle_alert(topic: str, payload: DetectionEvent) -> None:
        detections.append(payload)
        event.set()

    bus.subscribe("process.alert.detected", handle_alert)

    await module.start()
    detection = DetectionEvent(
        camera_id="front",
        detector_id="yolo",
        attributes={"label": "person", "bbox": (0, 0, 1, 1)},
    )
    await bus.publish("process.yolo.detected", detection)
    await asyncio.wait_for(event.wait(), timeout=1.0)
    event.clear()

    fake_time[0] += 1.0
    await bus.publish("process.yolo.detected", detection)
    await asyncio.sleep(0.05)

    await module.stop()
    await bus.stop()

    assert len(detections) == 1
