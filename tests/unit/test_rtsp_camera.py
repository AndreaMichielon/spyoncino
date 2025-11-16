import asyncio

import numpy as np
import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import Frame, ModuleConfig
from spyoncino.modules.input.rtsp_camera import RtspCamera


class FakeRtspClient:
    def __init__(self, frames: list[np.ndarray]) -> None:
        self._frames = frames
        self._index = 0

    async def connect(self) -> None:  # pragma: no cover - nothing to do
        return None

    async def read(self) -> np.ndarray | None:
        if self._index >= len(self._frames):
            await asyncio.sleep(0)
            return None
        frame = self._frames[self._index]
        self._index += 1
        return frame

    async def close(self) -> None:  # pragma: no cover - nothing to teardown
        return None


@pytest.mark.asyncio
async def test_rtsp_camera_emits_frames(tmp_path) -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    frame = np.full((4, 4, 3), 255, dtype=np.uint8)
    client = FakeRtspClient([frame])

    def factory(url: str) -> FakeRtspClient:
        return client

    module = RtspCamera(client_factory=factory)
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "camera_id": "lab",
                "rtsp_url": "rtsp://example.test/stream",
                "fps": 0,
                "buffer_dir": str(tmp_path),
            }
        )
    )

    received = asyncio.Event()
    frames: list[Frame] = []

    async def handler(topic: str, payload: Frame) -> None:
        frames.append(payload)
        received.set()

    bus.subscribe("camera.lab.frame", handler)
    await module.start()
    await asyncio.wait_for(received.wait(), timeout=0.2)
    await module.stop()
    await bus.stop()

    assert frames and frames[0].camera_id == "lab"
