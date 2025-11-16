"""
USB camera module responsible for ingesting frames from a local capture device.

The implementation mirrors the RTSP camera module so it can plug into the
existing orchestrator and event bus without additional glue code.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import io
import logging
from collections.abc import Callable
from pathlib import Path

import imageio.v3 as iio
import numpy as np

from ...core.contracts import BaseModule, Frame, ModuleConfig

logger = logging.getLogger(__name__)


class UsbCaptureClient:
    """Thin wrapper around OpenCV VideoCapture for USB devices."""

    def __init__(
        self,
        source: int | str,
        *,
        frame_width: int | None = None,
        frame_height: int | None = None,
    ) -> None:
        self._source = source
        self._frame_width = frame_width
        self._frame_height = frame_height
        self._capture = None

    async def connect(self) -> None:
        import cv2  # local import to keep optional during tests

        def _open() -> None:
            capture = cv2.VideoCapture(self._source)
            if not capture.isOpened():
                raise RuntimeError(f"Failed to open USB camera source {self._source!r}")
            native_width = float(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0.0
            native_height = float(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0.0
            aspect_ratio = None
            if native_width > 0 and native_height > 0:
                aspect_ratio = native_width / native_height
            target_width = self._frame_width
            target_height = self._frame_height
            if aspect_ratio:
                if target_width is not None and target_height is None:
                    target_height = int(round(target_width / aspect_ratio))
                    self._frame_height = target_height
                elif target_height is not None and target_width is None:
                    target_width = int(round(target_height * aspect_ratio))
                    self._frame_width = target_width
            if target_width is not None:
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(target_width))
            if target_height is not None:
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(target_height))
            self._capture = capture

        await asyncio.to_thread(_open)

    async def read(self) -> np.ndarray | None:
        if not self._capture:
            return None

        def _read() -> np.ndarray | None:
            assert self._capture
            ok, frame = self._capture.read()
            if not ok:
                return None
            return frame

        return await asyncio.to_thread(_read)

    async def close(self) -> None:
        if not self._capture:
            return

        def _release() -> None:
            assert self._capture
            self._capture.release()
            self._capture = None

        await asyncio.to_thread(_release)


class UsbCamera(BaseModule):
    """Input module that acquires frames from a USB capture device."""

    name = "modules.input.usb_camera"

    def __init__(
        self,
        *,
        client_factory: Callable[[int | str, int | None, int | None], UsbCaptureClient]
        | None = None,
    ) -> None:
        super().__init__()
        self._client_factory = client_factory or (
            lambda source, width, height: UsbCaptureClient(
                source,
                frame_width=width,
                frame_height=height,
            )
        )
        self._client: UsbCaptureClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = asyncio.Event()
        self._camera_id = "usb"
        self._source: int | str | None = 0
        self._fps: float | None = 15.0
        self._encoding = ".jpg"
        self._max_retries = 5
        self._retry_backoff = 1.0
        self._frame_width: int | None = None
        self._frame_height: int | None = None
        self._sequence = 0
        self._drop_blank_frames = False
        self._buffer_dir = Path("recordings") / "frames" / self._camera_id

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._camera_id = options.get("camera_id", self._camera_id)
        if "device_path" in options:
            self._source = options["device_path"]
        elif "device_index" in options:
            self._source = int(options["device_index"])
        elif not isinstance(self._source, int):
            self._source = 0
        if "fps" in options:
            fps_value = options["fps"]
            self._fps = float(fps_value) if fps_value is not None else None
        encoding = options.get("encoding", self._encoding)
        self._encoding = encoding if encoding.startswith(".") else f".{encoding}"
        self._max_retries = int(options.get("max_retries", self._max_retries))
        self._retry_backoff = float(options.get("retry_backoff", self._retry_backoff))
        width = options.get("frame_width", self._frame_width)
        self._frame_width = int(width) if width is not None else None
        height = options.get("frame_height", self._frame_height)
        self._frame_height = int(height) if height is not None else None
        self._drop_blank_frames = bool(options.get("drop_blank_frames", self._drop_blank_frames))
        self._buffer_dir = Path(
            options.get("buffer_dir", Path("recordings") / "frames" / self._camera_id)
        )

    async def start(self) -> None:
        if self._source is None:
            raise RuntimeError("UsbCamera requires a device_index or device_path.")
        self._client = self._client_factory(self._source, self._frame_width, self._frame_height)
        await self._client.connect()
        self._buffer_dir.mkdir(parents=True, exist_ok=True)
        self._running.set()
        self._task = asyncio.create_task(self._run(), name=f"{self.name}-loop")
        logger.info("USB camera %s connected to source %s", self._camera_id, self._source)

    async def stop(self) -> None:
        self._running.clear()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._client:
            await self._client.close()
            self._client = None
        logger.info("USB camera %s stopped", self._camera_id)

    async def _run(self) -> None:
        retry_count = 0
        last_failure_reason = "read failures"
        assert self._client is not None
        frame_interval = 0.0
        if self._fps:
            frame_interval = 0.0 if self._fps <= 0 else 1.0 / self._fps
        while self._running.is_set():
            frame = await self._client.read()
            failure_reason = "read failures"
            if frame is None:
                failure_reason = "read failures"
            elif self._drop_blank_frames and self._is_blank_frame(frame):
                failure_reason = "blank frames"
                frame = None

            if frame is None:
                retry_count += 1
                last_failure_reason = failure_reason
                if retry_count > self._max_retries:
                    logger.warning(
                        "USB camera %s encountered %d consecutive %s; reconnecting.",
                        self._camera_id,
                        retry_count,
                        last_failure_reason,
                    )
                    await self._client.close()
                    await asyncio.sleep(self._retry_backoff)
                    await self._client.connect()
                    retry_count = 0
                else:
                    await asyncio.sleep(frame_interval or self._retry_backoff)
                continue

            retry_count = 0
            last_failure_reason = "read failures"
            frame_bytes, metadata = self._encode_frame(frame)
            self._sequence += 1
            encoding_lower = self._encoding.lower()
            if encoding_lower in {".jpg", ".jpeg"}:
                content_type = "image/jpeg"
            elif encoding_lower == ".png":
                content_type = "image/png"
            else:
                content_type = "application/octet-stream"
            payload = Frame(
                camera_id=self._camera_id,
                sequence_id=self._sequence,
                data_ref=str(self._persist_frame(frame_bytes)),
                metadata=metadata,
                image_bytes=None,
                content_type=content_type,
            )
            await self.bus.publish(f"camera.{self._camera_id}.frame", payload)
            if frame_interval:
                await asyncio.sleep(frame_interval)

    def _encode_frame(self, frame: np.ndarray) -> tuple[bytes, dict[str, int]]:
        height, width = frame.shape[:2]
        with io.BytesIO() as buffer:
            iio.imwrite(buffer, frame, extension=self._encoding)
            body = buffer.getvalue()
        metadata = {"width": width, "height": height, "source": str(self._source)}
        return body, metadata

    @staticmethod
    def _is_blank_frame(frame: np.ndarray) -> bool:
        if frame.size == 0:
            return True
        return not frame.any()

    def _persist_frame(self, frame_bytes: bytes) -> Path:
        timestamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{self._camera_id}_{self._sequence}_{timestamp}{self._encoding}"
        path = self._buffer_dir / filename
        path.write_bytes(frame_bytes)
        return path.resolve()


__all__ = ["UsbCamera", "UsbCaptureClient"]
