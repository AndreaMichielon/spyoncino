"""Tests for the USB camera capture helpers."""

from __future__ import annotations

import sys
import types

import pytest

from spyoncino.modules.input.usb_camera import UsbCaptureClient


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
