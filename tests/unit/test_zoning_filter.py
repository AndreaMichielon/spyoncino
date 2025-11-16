import asyncio

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import DetectionEvent, ModuleConfig
from spyoncino.modules.process.zoning_filter import ZoningFilter


@pytest.mark.asyncio
async def test_zoning_filter_announces_matches() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    module = ZoningFilter()
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "enabled": True,
                "input_topic": "process.motion.unique",
                "output_topic": "process.motion.zoned",
                "zones": [
                    {
                        "camera_id": "lab",
                        "zone_id": "door",
                        "bounds": [0.0, 0.0, 1.0, 1.0],
                        "labels": ["person"],
                    }
                ],
            }
        )
    )

    detections: list[DetectionEvent] = []
    received = asyncio.Event()

    async def handler(topic: str, payload: DetectionEvent) -> None:
        detections.append(payload)
        received.set()

    bus.subscribe("process.motion.zoned", handler)
    await module.start()

    await bus.publish(
        "process.motion.unique",
        DetectionEvent(
            camera_id="lab",
            detector_id="yolo",
            attributes={
                "label": "person",
                "bbox": (10, 10, 50, 50),
                "frame": {"width": 100, "height": 100},
            },
        ),
    )

    await asyncio.wait_for(received.wait(), timeout=0.5)
    await module.stop()
    await bus.stop()

    assert detections
    assert detections[0].attributes["zone_matches"][0]["zone_id"] == "door"


@pytest.mark.asyncio
async def test_zoning_filter_uses_camera_dimensions_when_frame_missing() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    module = ZoningFilter()
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "enabled": True,
                "input_topic": "process.motion.unique",
                "output_topic": "process.motion.zoned",
                "camera_dimensions": {"lab": {"width": 200, "height": 50}},
                "zones": [
                    {
                        "camera_id": "lab",
                        "zone_id": "center",
                        "bounds": [0.4, 0.2, 0.6, 0.4],
                        "labels": ["person"],
                    }
                ],
            }
        )
    )

    detections: list[DetectionEvent] = []
    received = asyncio.Event()

    async def handler(topic: str, payload: DetectionEvent) -> None:
        detections.append(payload)
        received.set()

    bus.subscribe("process.motion.zoned", handler)
    await module.start()

    await bus.publish(
        "process.motion.unique",
        DetectionEvent(
            camera_id="lab",
            detector_id="yolo",
            attributes={
                "label": "person",
                "bbox": (60, 5, 140, 25),
            },
        ),
    )

    await asyncio.wait_for(received.wait(), timeout=0.5)
    await module.stop()
    await bus.stop()

    assert detections
    assert detections[0].attributes["zone_matches"][0]["zone_id"] == "center"
