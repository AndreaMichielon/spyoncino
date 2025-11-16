"""
Simple in-memory camera simulator for early integration testing.

The module periodically publishes `Frame` payloads to the event bus so the
pipeline can be exercised without connecting to physical hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import io
import itertools
import logging
from pathlib import Path

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
        # Test-oriented robustness helpers (configurable):
        # - Emit two frames immediately on startup to establish baseline+diff
        # - Wait a small delay for downstream subscriptions before first emit
        self._bootstrap_two_frames: bool = True
        self._startup_delay_seconds: float = 0.1
        self._sequence = itertools.count()
        self._task: asyncio.Task[None] | None = None
        self._running = asyncio.Event()
        self._buffer_dir = Path("recordings") / "frames" / self._camera_id

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._camera_id = options.get("camera_id", self._camera_id)
        self._interval = float(options.get("interval_seconds", self._interval))
        self._frame_width = int(options.get("frame_width", self._frame_width))
        self._frame_height = int(options.get("frame_height", self._frame_height))
        self._buffer_dir = Path(
            options.get("buffer_dir", Path("recordings") / "frames" / self._camera_id)
        )
        # Optional simulator knobs (safe defaults for tests; can be disabled for prod)
        self._bootstrap_two_frames = bool(
            options.get("bootstrap_two_frames", self._bootstrap_two_frames)
        )
        # Allow ms or seconds; accept either 'startup_delay_ms' or 'startup_delay_seconds'
        if "startup_delay_ms" in options:
            with contextlib.suppress(TypeError, ValueError):
                self._startup_delay_seconds = float(options["startup_delay_ms"]) / 1000.0
        else:
            self._startup_delay_seconds = float(
                options.get("startup_delay_seconds", self._startup_delay_seconds)
            )

    async def start(self) -> None:
        self._running.set()
        self._buffer_dir.mkdir(parents=True, exist_ok=True)
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
        bootstrapped = False
        first = True
        while self._running.is_set():
            if first:
                # Allow downstream modules to finish subscribing before first emit
                if self._startup_delay_seconds > 0:
                    await asyncio.sleep(self._startup_delay_seconds)
                first = False
            seq = next(self._sequence)
            image_bytes = self._render_frame_bytes(seq)
            frame = Frame(
                camera_id=self._camera_id,
                sequence_id=seq,
                data_ref=str(self._persist_frame(seq, image_bytes)),
                metadata={
                    "width": self._frame_width,
                    "height": self._frame_height,
                },
                image_bytes=None,
                content_type="image/png",
            )
            await self.bus.publish(f"camera.{self._camera_id}.frame", frame)
            # Publish a second immediate frame on first loop to establish baseline + diff quickly
            if self._bootstrap_two_frames and not bootstrapped and self._running.is_set():
                # Force a significantly different frame to easily exceed motion threshold
                seq = next(self._sequence)
                image_bytes = self._render_high_motion_frame_bytes()
                frame = Frame(
                    camera_id=self._camera_id,
                    sequence_id=seq,
                    data_ref=str(self._persist_frame(seq, image_bytes)),
                    metadata={
                        "width": self._frame_width,
                        "height": self._frame_height,
                    },
                    image_bytes=None,
                    content_type="image/png",
                )
                await self.bus.publish(f"camera.{self._camera_id}.frame", frame)
                bootstrapped = True
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

    def _render_high_motion_frame_bytes(self) -> bytes:
        """
        Generate a starkly different frame to guarantee a large diff for motion tests.
        """
        frame = np.full((self._frame_height, self._frame_width, 3), 255, dtype=np.uint8)
        with io.BytesIO() as buffer:
            iio.imwrite(buffer, frame, extension=".png")
            return buffer.getvalue()

    def _persist_frame(self, sequence: int, image_bytes: bytes) -> Path:
        timestamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{self._camera_id}_{sequence}_{timestamp}.png"
        path = self._buffer_dir / filename
        path.write_bytes(image_bytes)
        return path.resolve()
