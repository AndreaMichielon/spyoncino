"""
Simple in-memory camera simulator for early integration testing.

The module periodically publishes `Frame` payloads to the event bus so the
pipeline can be exercised without connecting to physical hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging

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
    ) -> None:
        super().__init__()
        self._camera_id = camera_id
        self._interval = interval_seconds
        self._sequence = itertools.count()
        self._task: asyncio.Task[None] | None = None
        self._running = asyncio.Event()

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._camera_id = options.get("camera_id", self._camera_id)
        self._interval = float(options.get("interval_seconds", self._interval))

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
            frame = Frame(
                camera_id=self._camera_id,
                sequence_id=seq,
                data_ref=f"memory://frame/{self._camera_id}/{seq}",
            )
            await self.bus.publish(f"camera.{self._camera_id}.frame", frame)
            await asyncio.sleep(self._interval)
