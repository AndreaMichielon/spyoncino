import asyncio

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import BasePayload, Frame, ModuleConfig
from spyoncino.modules.input.camera_sim import CameraSimulator
from spyoncino.modules.process.motion_detector import MotionDetector


@pytest.mark.asyncio
async def test_bus_handles_sync_handlers_and_drains_on_stop() -> None:
    bus = EventBus(telemetry_enabled=False)
    received: list[BasePayload] = []

    # Sync handler (not async)
    def sync_handler(topic: str, payload: BasePayload) -> None:
        received.append(payload)

    bus.subscribe("topic.sync", sync_handler)  # type: ignore[arg-type]
    await bus.start()
    await bus.publish("topic.sync", Frame(camera_id="x"))

    # Give dispatcher a tiny slice
    await asyncio.sleep(0.02)
    assert received, "Expected sync handler to receive payload without errors"

    await bus.stop()  # Should not raise and should drain


@pytest.mark.asyncio
async def test_motion_detector_emits_on_second_frame_quickly() -> None:
    import io

    import imageio.v3 as iio
    import numpy as np

    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    det = MotionDetector()
    det.set_bus(bus)
    await det.configure(
        ModuleConfig(
            options={
                "input_topics": ["camera.quick.frame"],
                "interval_seconds": 0.0,
                "motion_threshold": 5,
            }
        )
    )
    await det.start()

    signaled = asyncio.Event()

    async def on_detect(topic: str, payload: BasePayload) -> None:
        signaled.set()

    bus.subscribe("process.motion.detected", on_detect)

    # Publish two frames back-to-back; the second should trigger quickly
    def _png_bytes(arr: "np.ndarray") -> bytes:
        with io.BytesIO() as buf:
            iio.imwrite(buf, arr, extension=".png")
            return buf.getvalue()

    base_img = np.zeros((16, 16, 3), dtype=np.uint8)
    diff_img = np.full((16, 16, 3), 255, dtype=np.uint8)
    base = Frame(camera_id="quick", image_bytes=_png_bytes(base_img), content_type="image/png")
    diff = Frame(camera_id="quick", image_bytes=_png_bytes(diff_img), content_type="image/png")
    await bus.publish("camera.quick.frame", base)
    await bus.publish("camera.quick.frame", diff)

    await asyncio.wait_for(signaled.wait(), timeout=0.5)
    await det.stop()
    await bus.stop()


@pytest.mark.asyncio
async def test_camera_simulator_bootstrap_two_frames() -> None:
    """
    Ensure the simulator emits two frames promptly when bootstrap_two_frames is enabled.
    """
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    sim = CameraSimulator()
    sim.set_bus(bus)
    await sim.configure(
        ModuleConfig(
            options={
                "camera_id": "sim",
                "interval_seconds": 0.2,
                "bootstrap_two_frames": True,
                "startup_delay_ms": 10,
            }
        )
    )

    count = 0
    event = asyncio.Event()

    async def on_frame(topic: str, payload: BasePayload) -> None:
        nonlocal count
        count += 1
        if count >= 2:
            event.set()

    bus.subscribe("camera.sim.frame", on_frame)
    await sim.start()

    await asyncio.wait_for(event.wait(), timeout=0.5)
    await sim.stop()
    await bus.stop()
