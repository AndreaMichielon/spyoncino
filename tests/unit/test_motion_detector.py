import asyncio
import io

import imageio.v3 as iio
import numpy as np
import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import DetectionEvent, Frame, ModuleConfig
from spyoncino.modules.process.motion_detector import MotionDetector


def _png_bytes_from_array(array: np.ndarray) -> bytes:
    with io.BytesIO() as buffer:
        iio.imwrite(buffer, array, extension=".png")
        return buffer.getvalue()


@pytest.mark.asyncio
async def test_motion_detector_skips_when_no_image_bytes() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    mod = MotionDetector()
    mod.set_bus(bus)
    await mod.configure(
        ModuleConfig(
            options={
                "input_topics": ["camera.cam1.frame"],
                "interval_seconds": 0.0,
                "motion_threshold": 5,
            }
        )
    )

    received: list[DetectionEvent] = []
    done = asyncio.Event()

    async def handler(topic: str, payload: DetectionEvent) -> None:
        received.append(payload)
        done.set()

    bus.subscribe("process.motion.detected", handler)
    await mod.start()

    # No image bytes -> ignored
    await bus.publish("camera.cam1.frame", Frame(camera_id="cam1"))
    # Give a short time slice to process
    await asyncio.sleep(0.05)

    await mod.stop()
    await bus.stop()
    assert received == []


@pytest.mark.asyncio
async def test_motion_detector_emits_on_above_threshold_motion() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    mod = MotionDetector()
    mod.set_bus(bus)
    await mod.configure(
        ModuleConfig(
            options={
                "input_topics": ["camera.cam2.frame"],
                "interval_seconds": 0.0,  # evaluate every opportunity
                "motion_threshold": 5,  # very low to trigger easily
            }
        )
    )

    events: list[DetectionEvent] = []
    signaled = asyncio.Event()

    async def handler(topic: str, payload: DetectionEvent) -> None:
        events.append(payload)
        signaled.set()

    bus.subscribe("process.motion.detected", handler)
    await mod.start()

    # First frame establishes baseline (no motion yet)
    base = np.zeros((16, 16, 3), dtype=np.uint8)
    await bus.publish(
        "camera.cam2.frame",
        Frame(camera_id="cam2", image_bytes=_png_bytes_from_array(base), content_type="image/png"),
    )
    await asyncio.sleep(0.02)

    # Second frame is very different -> should exceed threshold, emit one event
    diff = np.full((16, 16, 3), 255, dtype=np.uint8)
    await bus.publish(
        "camera.cam2.frame",
        Frame(camera_id="cam2", image_bytes=_png_bytes_from_array(diff), content_type="image/png"),
    )

    await asyncio.wait_for(signaled.wait(), timeout=0.5)

    await mod.stop()
    await bus.stop()

    assert len(events) == 1
    assert isinstance(events[0], DetectionEvent)
    assert events[0].camera_id == "cam2"
    assert "motion_percent" in (events[0].attributes or {})


@pytest.mark.asyncio
async def test_motion_detector_does_not_emit_below_threshold() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    mod = MotionDetector()
    mod.set_bus(bus)
    # Set an unrealistically high threshold so small changes do not trigger
    await mod.configure(
        ModuleConfig(
            options={
                "input_topics": ["camera.cam3.frame"],
                "interval_seconds": 0.0,
                "motion_threshold": 90,
            }
        )
    )

    events: list[DetectionEvent] = []
    # No event should be received; we won't wait on an Event, just allow processing window
    bus.subscribe("process.motion.detected", lambda t, p: events.append(p))  # type: ignore[arg-type]
    await mod.start()

    base = np.zeros((16, 16, 3), dtype=np.uint8)
    slightly_changed = base.copy()
    slightly_changed[0, 0, 0] = 10

    await bus.publish(
        "camera.cam3.frame",
        Frame(camera_id="cam3", image_bytes=_png_bytes_from_array(base), content_type="image/png"),
    )
    await asyncio.sleep(0.02)
    await bus.publish(
        "camera.cam3.frame",
        Frame(
            camera_id="cam3",
            image_bytes=_png_bytes_from_array(slightly_changed),
            content_type="image/png",
        ),
    )
    await asyncio.sleep(0.1)

    await mod.stop()
    await bus.stop()
    assert events == []
