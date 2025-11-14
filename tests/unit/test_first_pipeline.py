import asyncio

import pytest

from spyoncino.core.contracts import DetectionEvent, ModuleConfig
from spyoncino.core.orchestrator import Orchestrator
from spyoncino.modules import CameraSimulator, MotionDetector


@pytest.mark.asyncio
async def test_camera_to_motion_pipeline() -> None:
    orchestrator = Orchestrator()
    detection_signal = asyncio.Event()
    detections = []

    async def handle_detection(topic: str, payload: DetectionEvent) -> None:
        detections.append(payload)
        detection_signal.set()

    orchestrator.bus.subscribe("process.motion.detected", handle_detection)

    await orchestrator.add_module(
        CameraSimulator(),
        config=ModuleConfig(options={"interval_seconds": 0.01}),
    )
    await orchestrator.add_module(MotionDetector())

    await orchestrator.start()
    await asyncio.wait_for(detection_signal.wait(), timeout=0.3)
    await orchestrator.stop()

    assert detections
    assert detections[0].detector_id == "motion-basic"
