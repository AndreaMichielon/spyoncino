import asyncio
import io

import imageio.v3 as iio
import numpy as np
import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import DetectionEvent, Frame, ModuleConfig
from spyoncino.modules.process.yolo_detector import (
    DetectionCandidate,
    YoloDetector,
)


class FakePredictor:
    def predict(self, image: np.ndarray) -> list[DetectionCandidate]:
        return [
            DetectionCandidate(label="person", confidence=0.9, bbox=(0, 0, 2, 2)),
        ]


@pytest.mark.asyncio
async def test_yolo_detector_emits_detections() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    def factory(model_path: str | None) -> FakePredictor:
        return FakePredictor()

    module = YoloDetector(predictor_factory=factory)
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "input_topics": ["camera.lab.frame"],
                "output_topic": "process.yolo.detected",
                "confidence_threshold": 0.5,
                "class_filter": ["person"],
            }
        )
    )

    detections: list[DetectionEvent] = []
    detected = asyncio.Event()

    async def handler(topic: str, payload: DetectionEvent) -> None:
        detections.append(payload)
        detected.set()

    bus.subscribe("process.yolo.detected", handler)
    await module.start()

    frame_array = np.zeros((4, 4, 3), dtype=np.uint8)
    with io.BytesIO() as buffer:
        iio.imwrite(buffer, frame_array, extension=".png")
        image_bytes = buffer.getvalue()

    await bus.publish(
        "camera.lab.frame",
        Frame(camera_id="lab", image_bytes=image_bytes, content_type="image/png"),
    )

    await asyncio.wait_for(detected.wait(), timeout=0.2)
    await module.stop()
    await bus.stop()

    assert detections and detections[0].attributes["label"] == "person"
