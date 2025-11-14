"""
Simple in-memory camera simulator for early integration testing.

The module periodically publishes `Frame` payloads to the event bus so the
pipeline can be exercised without connecting to physical hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import itertools
import logging

import imageio.v3 as iio
import numpy as np

from ...core.contracts import BaseModule, Frame, ModuleConfig

logger = logging.getLogger(__name__)


class CameraSimulator(BaseModule):
    """Generates synthetic frames at a fixed cadence."""

    name = "modules.input.camera_simulator"

    def __init__(
        self,
        *,
        camera_id: str = "default",
        interval_seconds: float = 1.0,
        frame_width: int = 320,
        frame_height: int = 240,
    ) -> None:
        super().__init__()
        self._camera_id = camera_id
        self._interval = interval_seconds
        self._frame_width = frame_width
        self._frame_height = frame_height
        self._sequence = itertools.count()
        self._task: asyncio.Task[None] | None = None
        self._running = asyncio.Event()

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._camera_id = options.get("camera_id", self._camera_id)
        self._interval = float(options.get("interval_seconds", self._interval))
        self._frame_width = int(options.get("frame_width", self._frame_width))
        self._frame_height = int(options.get("frame_height", self._frame_height))

    async def start(self) -> None:
        self._running.set()
        self._task = asyncio.create_task(self._run(), name=f"{self.name}-publisher")
        logger.info(
            "CameraSimulator for camera %s emitting every %.2fs",
            self._camera_id,
            self._interval,
        )

    async def stop(self) -> None:
        self._running.clear()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("CameraSimulator for camera %s stopped", self._camera_id)

    async def _run(self) -> None:
        while self._running.is_set():
            seq = next(self._sequence)
            image_bytes = self._render_frame_bytes(seq)
            frame = Frame(
                camera_id=self._camera_id,
                sequence_id=seq,
                data_ref=f"memory://frame/{self._camera_id}/{seq}",
                metadata={
                    "width": self._frame_width,
                    "height": self._frame_height,
                },
                image_bytes=image_bytes,
                content_type="image/png",
            )
            await self.bus.publish(f"camera.{self._camera_id}.frame", frame)
            await asyncio.sleep(self._interval)

    def _render_frame_bytes(self, sequence: int) -> bytes:
        """
        Generate a simple synthetic frame encoded as PNG bytes.
        """
        red = (sequence * 17) % 255
        gradient_x = np.linspace(0, 255, self._frame_width, dtype=np.uint8)
        gradient_y = np.linspace(0, 255, self._frame_height, dtype=np.uint8)
        frame = np.zeros((self._frame_height, self._frame_width, 3), dtype=np.uint8)
        frame[..., 0] = red
        frame[..., 1] = gradient_x[np.newaxis, :]
        frame[..., 2] = gradient_y[:, np.newaxis]
        with io.BytesIO() as buffer:
            iio.imwrite(buffer, frame, extension=".png")
            return buffer.getvalue()
