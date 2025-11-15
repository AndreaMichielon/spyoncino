"""Tests for the USB camera capture helpers."""

from __future__ import annotations

import asyncio
import sys
import types

import numpy as np
import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import Frame, ModuleConfig
from spyoncino.modules.input.usb_camera import UsbCamera, UsbCaptureClient


def _install_fake_cv2(monkeypatch: pytest.MonkeyPatch, *, width: float, height: float):
    """Install a fake cv2 module that records capture calls."""

    captures: list[types.SimpleNamespace] = []
    cap_prop_width = 3
    cap_prop_height = 4

    class FakeCapture:
        def __init__(self, source: int | str) -> None:
            self.source = source
            self.set_calls: list[tuple[int, float]] = []

        def isOpened(self) -> bool:  # noqa: N802 - mirrors cv2 API
            return True

        def set(self, prop: int, value: float) -> None:
            self.set_calls.append((prop, value))

        def get(self, prop: int) -> float:
            if prop == cap_prop_width:
                return width
            if prop == cap_prop_height:
                return height
            return 0.0

        def release(self) -> None:
            return None

    def video_capture(source: int | str) -> FakeCapture:
        capture = FakeCapture(source)
        captures.append(capture)
        return capture

    fake_cv2 = types.SimpleNamespace(
        CAP_PROP_FRAME_WIDTH=cap_prop_width,
        CAP_PROP_FRAME_HEIGHT=cap_prop_height,
        VideoCapture=video_capture,
        captures=captures,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    return fake_cv2


@pytest.mark.asyncio
async def test_usb_capture_client_backfills_missing_height(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cv2 = _install_fake_cv2(monkeypatch, width=1920.0, height=1080.0)
    client = UsbCaptureClient(0, frame_width=640, frame_height=None)

    await client.connect()

    capture = fake_cv2.captures[0]
    assert capture.set_calls == [
        (fake_cv2.CAP_PROP_FRAME_WIDTH, 640.0),
        (fake_cv2.CAP_PROP_FRAME_HEIGHT, 360.0),
    ]


@pytest.mark.asyncio
async def test_usb_capture_client_backfills_missing_width(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cv2 = _install_fake_cv2(monkeypatch, width=1920.0, height=1080.0)
    client = UsbCaptureClient(0, frame_width=None, frame_height=360)

    await client.connect()

    capture = fake_cv2.captures[0]
    assert capture.set_calls == [
        (fake_cv2.CAP_PROP_FRAME_WIDTH, 640.0),
        (fake_cv2.CAP_PROP_FRAME_HEIGHT, 360.0),
    ]


@pytest.mark.asyncio
async def test_usb_capture_client_respects_native_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cv2 = _install_fake_cv2(monkeypatch, width=1920.0, height=1080.0)
    client = UsbCaptureClient(0, frame_width=None, frame_height=None)

    await client.connect()

    capture = fake_cv2.captures[0]
    assert capture.set_calls == []


class RecordingUsbClient:
    def __init__(self, frames: list[np.ndarray | None]) -> None:
        self.frames = frames
        self.connect_calls = 0
        self.close_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def read(self) -> np.ndarray | None:
        if not self.frames:
            await asyncio.sleep(0)
            return None
        frame = self.frames.pop(0)
        if frame is None:
            await asyncio.sleep(0)
            return None
        return frame

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_usb_camera_publishes_frames_with_metadata() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    frame = np.full((2, 2, 3), 255, dtype=np.uint8)
    client = RecordingUsbClient([frame])

    captured_factory_args: dict[str, int | str | None] = {}

    def factory(source: int | str, width: int | None, height: int | None) -> RecordingUsbClient:
        captured_factory_args.update({"source": source, "width": width, "height": height})
        return client

    camera = UsbCamera(client_factory=factory)
    camera.set_bus(bus)
    await camera.configure(
        ModuleConfig(
            options={
                "camera_id": "lab",
                "device_path": "/dev/video0",
                "frame_width": 320,
                "frame_height": 240,
                "encoding": "png",
                "fps": 0,
            }
        )
    )

    received = asyncio.Event()
    frames: list[Frame] = []

    async def handler(topic: str, payload: Frame) -> None:
        frames.append(payload)
        received.set()

    bus.subscribe("camera.lab.frame", handler)
    await camera.start()
    await asyncio.wait_for(received.wait(), timeout=0.5)
    await camera.stop()
    await bus.stop()

    assert captured_factory_args == {"source": "/dev/video0", "width": 320, "height": 240}
    assert frames
    payload = frames[0]
    assert payload.content_type == "image/png"
    assert payload.metadata["source"] == "/dev/video0"
    assert payload.metadata["width"] == 2
    assert payload.metadata["height"] == 2


@pytest.mark.asyncio
async def test_usb_camera_reconnects_after_consecutive_failures() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    client = RecordingUsbClient([None, None, frame])

    def factory(source: int | str, width: int | None, height: int | None) -> RecordingUsbClient:
        return client

    camera = UsbCamera(client_factory=factory)
    camera.set_bus(bus)
    await camera.configure(
        ModuleConfig(
            options={
                "camera_id": "garage",
                "device_index": 1,
                "max_retries": 1,
                "retry_backoff": 0.01,
                "fps": None,
            }
        )
    )

    received = asyncio.Event()

    async def handler(topic: str, payload: Frame) -> None:
        received.set()

    bus.subscribe("camera.garage.frame", handler)
    await camera.start()
    await asyncio.wait_for(received.wait(), timeout=0.5)
    await camera.stop()
    await bus.stop()

    assert client.connect_calls >= 2  # initial + reconnect
    assert client.close_calls >= 1
