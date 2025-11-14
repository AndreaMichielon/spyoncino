import asyncio
import io
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import DetectionEvent, Frame, ModuleConfig, SnapshotArtifact
from spyoncino.modules.event.gif_builder import GifBuilder


def _frame_bytes() -> bytes:
    array = np.random.randint(0, 255, size=(4, 4, 3), dtype=np.uint8)
    with io.BytesIO() as buffer:
        iio.imwrite(buffer, array, extension=".png")
        return buffer.getvalue()


@pytest.mark.asyncio
async def test_gif_builder_creates_artifacts(tmp_path: Path) -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    module = GifBuilder()
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "frame_topics": ["camera.lab.frame"],
                "detection_topic": "process.yolo.detected",
                "output_topic": "event.gif.ready",
                "output_dir": str(tmp_path),
                "fps": 5,
                "duration_seconds": 1,
                "max_frames": 5,
            }
        )
    )

    artifacts: list[SnapshotArtifact] = []
    ready = asyncio.Event()

    async def handler(topic: str, payload: SnapshotArtifact) -> None:
        artifacts.append(payload)
        ready.set()

    bus.subscribe("event.gif.ready", handler)
    await module.start()

    for _ in range(3):
        await bus.publish(
            "camera.lab.frame",
            Frame(camera_id="lab", image_bytes=_frame_bytes(), content_type="image/png"),
        )

    await bus.publish(
        "process.yolo.detected",
        DetectionEvent(camera_id="lab", detector_id="unit-test", frame_ref="mem::1"),
    )

    await asyncio.wait_for(ready.wait(), timeout=0.5)
    await module.stop()
    await bus.stop()

    assert artifacts
    gif_path = Path(artifacts[0].artifact_path)
    assert gif_path.exists()
    assert artifacts[0].content_type == "image/gif"
