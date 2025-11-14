import asyncio
from pathlib import Path

import pytest

from spyoncino.core.config import ConfigService
from spyoncino.core.contracts import DetectionEvent, SnapshotArtifact
from spyoncino.core.orchestrator import Orchestrator
from spyoncino.modules import CameraSimulator, MotionDetector, SnapshotWriter


@pytest.mark.asyncio
async def test_camera_to_snapshot_pipeline(sample_config_service: ConfigService) -> None:
    orchestrator = Orchestrator()
    detection_signal = asyncio.Event()
    snapshot_signal = asyncio.Event()
    detections: list[DetectionEvent] = []
    artifacts: list[SnapshotArtifact] = []

    async def handle_detection(topic: str, payload: DetectionEvent) -> None:
        detections.append(payload)
        detection_signal.set()

    async def handle_snapshot(topic: str, payload: SnapshotArtifact) -> None:
        artifacts.append(payload)
        snapshot_signal.set()

    orchestrator.bus.subscribe("process.motion.detected", handle_detection)
    orchestrator.bus.subscribe("event.snapshot.ready", handle_snapshot)

    await orchestrator.add_module(
        CameraSimulator(),
        config=sample_config_service.module_config_for("modules.input.camera_simulator"),
    )
    await orchestrator.add_module(
        MotionDetector(),
        config=sample_config_service.module_config_for("modules.process.motion_detector"),
    )
    await orchestrator.add_module(
        SnapshotWriter(),
        config=sample_config_service.module_config_for("modules.event.snapshot_writer"),
    )

    await orchestrator.start()
    await asyncio.wait_for(snapshot_signal.wait(), timeout=1.0)
    await orchestrator.stop()

    assert detections
    assert detections[0].detector_id == "motion-basic"
    assert artifacts
    snapshot_path = Path(artifacts[0].artifact_path)
    assert snapshot_path.exists()
