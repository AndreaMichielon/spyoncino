import asyncio
import io
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import DetectionEvent, Frame, MediaArtifact, ModuleConfig
from spyoncino.modules.event.clip_builder import ClipBuilder


def _frame_bytes() -> bytes:
    array = np.random.randint(0, 255, size=(8, 8, 3), dtype=np.uint8)
    with io.BytesIO() as buffer:
        iio.imwrite(buffer, array, extension=".png")
        return buffer.getvalue()


@pytest.mark.asyncio
async def test_clip_builder_emits_media_artifact(tmp_path: Path) -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    module = ClipBuilder()
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "enabled": True,
                "frame_topics": ["camera.lab.frame"],
                "detection_topic": "process.motion.unique",
                "output_topic": "event.clip.ready",
                "output_dir": str(tmp_path),
                "fps": 4,
                "duration_seconds": 0.5,
                "max_artifacts": 2,
            }
        )
    )

    artifacts: list[MediaArtifact] = []
    ready = asyncio.Event()

    async def handler(topic: str, payload: MediaArtifact) -> None:
        artifacts.append(payload)
        ready.set()

    bus.subscribe("event.clip.ready", handler)
    await module.start()

    for seq in range(4):
        await bus.publish(
            "camera.lab.frame",
            Frame(
                camera_id="lab",
                sequence_id=seq,
                image_bytes=_frame_bytes(),
                content_type="image/png",
            ),
        )

    await bus.publish(
        "process.motion.unique",
        DetectionEvent(
            camera_id="lab",
            detector_id="unit-test",
            attributes={"label": "person"},
        ),
    )

    await asyncio.wait_for(ready.wait(), timeout=1.0)
    await module.stop()
    await bus.stop()

    assert artifacts
    clip_path = Path(artifacts[0].artifact_path)
    assert clip_path.exists()
    assert artifacts[0].media_kind == "clip"
