"""
RTSP camera module that streams frames from a network source.

The module focuses on resiliency and testability by allowing a pluggable
frame client so integration tests can inject deterministic frame sources.
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


class RtspClient:
    """
    Lightweight interface for RTSP frame sources.

    Default implementation relies on OpenCV, but tests can inject in-memory
    mocks without requiring camera hardware.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._capture = None

    async def connect(self) -> None:
        import cv2  # local import to avoid hard dependency during unit tests

        def _open() -> None:
            capture = cv2.VideoCapture(self._url)
            if not capture.isOpened():
                raise RuntimeError(f"Failed to open RTSP stream {self._url}")
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


class RtspCamera(BaseModule):
    """Input module that ingests frames from an RTSP endpoint."""

    name = "modules.input.rtsp_camera"

    def __init__(
        self,
        *,
        client_factory: Callable[[str], RtspClient] | None = None,
    ) -> None:
        super().__init__()
        self._client_factory = client_factory or RtspClient
        self._client: RtspClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = asyncio.Event()
        self._camera_id = "rtsp"
        self._rtsp_url = ""
        self._fps = 15.0
        self._encoding = ".jpg"
        self._max_retries = 5
        self._retry_backoff = 1.0
        self._sequence = 0
        self._buffer_dir = Path("recordings") / "frames" / self._camera_id

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._camera_id = options.get("camera_id", self._camera_id)
        self._rtsp_url = options.get("rtsp_url", self._rtsp_url)
        self._fps = float(options.get("fps", self._fps))
        encoding = options.get("encoding", self._encoding)
        self._encoding = encoding if encoding.startswith(".") else f".{encoding}"
        self._max_retries = int(options.get("max_retries", self._max_retries))
        self._retry_backoff = float(options.get("retry_backoff", self._retry_backoff))
        self._buffer_dir = Path(
            options.get("buffer_dir", Path("recordings") / "frames" / self._camera_id)
        )

    async def start(self) -> None:
        if not self._rtsp_url:
            raise RuntimeError("RtspCamera requires an rtsp_url to be configured.")
        self._client = self._client_factory(self._rtsp_url)
        await self._client.connect()
        self._buffer_dir.mkdir(parents=True, exist_ok=True)
        self._running.set()
        self._task = asyncio.create_task(self._run(), name=f"{self.name}-loop")
        logger.info("RTSP camera %s connected to %s", self._camera_id, self._rtsp_url)

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
        logger.info("RTSP camera %s stopped", self._camera_id)

    async def _run(self) -> None:
        retry_count = 0
        assert self._client is not None
        frame_interval = 0.0 if self._fps <= 0 else 1.0 / self._fps
        while self._running.is_set():
            frame = await self._client.read()
            if frame is None:
                retry_count += 1
                if retry_count > self._max_retries:
                    logger.warning(
                        "RTSP camera %s experienced %d consecutive read errors; reconnecting.",
                        self._camera_id,
                        retry_count,
                    )
                    await self._client.close()
                    await asyncio.sleep(self._retry_backoff)
                    await self._client.connect()
                    retry_count = 0
                else:
                    await asyncio.sleep(frame_interval or self._retry_backoff)
                continue

            retry_count = 0
            frame_bytes, metadata = self._encode_frame(frame)
            self._sequence += 1
            encoding_lower = self._encoding.lower()
            if encoding_lower in {".jpg", ".jpeg"}:
                content_type = "image/jpeg"
            elif encoding_lower == ".png":
                content_type = "image/png"
            else:
                content_type = "application/octet-stream"
            # Persist to rolling buffer on disk and publish reference only
            timestamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{self._camera_id}_{self._sequence}_{timestamp}{self._encoding}"
            path = self._buffer_dir / filename
            await asyncio.to_thread(path.write_bytes, frame_bytes)
            payload = Frame(
                camera_id=self._camera_id,
                sequence_id=self._sequence,
                data_ref=str(path.resolve()),
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
        metadata = {"width": width, "height": height}
        return body, metadata


__all__ = ["RtspCamera", "RtspClient"]
