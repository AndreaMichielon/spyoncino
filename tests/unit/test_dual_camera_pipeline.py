import asyncio
from pathlib import Path

import pytest

from spyoncino.core.contracts import ModuleConfig, SnapshotArtifact
from spyoncino.core.orchestrator import Orchestrator
from spyoncino.modules import (
    CameraSimulator,
    EventDeduplicator,
    MotionDetector,
    RateLimiter,
    SnapshotWriter,
)


@pytest.mark.asyncio
async def test_dual_camera_pipeline(tmp_path: Path) -> None:
    orchestrator = Orchestrator(health_interval=1.0)
    snapshots: set[str] = set()
    snapshot_signal = asyncio.Event()

    async def handle_snapshot(topic: str, payload: SnapshotArtifact) -> None:
        snapshots.add(payload.camera_id)
        if len(snapshots) >= 2:
            snapshot_signal.set()

    orchestrator.bus.subscribe("event.snapshot.allowed", handle_snapshot)

    await orchestrator.add_module(
        CameraSimulator(),
        ModuleConfig(
            options={
                "camera_id": "front",
                "interval_seconds": 0.05,
                "frame_width": 64,
                "frame_height": 48,
            }
        ),
    )
    await orchestrator.add_module(
        CameraSimulator(),
        ModuleConfig(
            options={
                "camera_id": "back",
                "interval_seconds": 0.05,
                "frame_width": 64,
                "frame_height": 48,
            }
        ),
    )
    await orchestrator.add_module(
        MotionDetector(),
        ModuleConfig(options={"input_topic": "camera.front.frame", "detector_id": "front-det"}),
    )
    await orchestrator.add_module(
        MotionDetector(),
        ModuleConfig(options={"input_topic": "camera.back.frame", "detector_id": "back-det"}),
    )
    await orchestrator.add_module(
        EventDeduplicator(),
        ModuleConfig(
            options={
                "input_topic": "process.motion.detected",
                "output_topic": "process.motion.unique",
                "window_seconds": 0.1,
            }
        ),
    )
    await orchestrator.add_module(
        SnapshotWriter(),
        ModuleConfig(
            options={
                "frame_topics": ["camera.front.frame", "camera.back.frame"],
                "detection_topic": "process.motion.unique",
                "output_dir": str(tmp_path),
            }
        ),
    )
    await orchestrator.add_module(
        RateLimiter(),
        ModuleConfig(
            options={
                "input_topic": "event.snapshot.ready",
                "output_topic": "event.snapshot.allowed",
                "max_events": 10,
                "per_seconds": 1,
            }
        ),
    )

    await orchestrator.start()
    await asyncio.wait_for(snapshot_signal.wait(), timeout=2.0)
    await orchestrator.stop()

    assert snapshots == {"front", "back"}
