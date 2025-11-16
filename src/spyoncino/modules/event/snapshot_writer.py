"""
Persist snapshots to disk whenever detections are emitted.

The module listens for raw frames and detection events, correlates them by
camera, and writes an image artifact that downstream modules (e.g. Telegram
notifier) can publish to users.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import re
from collections.abc import Callable
from pathlib import Path

import cv2
import imageio.v3 as iio
import numpy as np

from ...core.bus import Subscription
from ...core.contracts import (
    BaseModule,
    DetectionEvent,
    Frame,
    ModuleConfig,
    SnapshotArtifact,
)

logger = logging.getLogger(__name__)
_CAMERA_SAFE = re.compile(r"[^A-Za-z0-9_-]")


class SnapshotWriter(BaseModule):
    """Write snapshot files and publish `event.snapshot.ready` payloads."""

    name = "modules.event.snapshot_writer"

    def __init__(self, *, clock: Callable[[], dt.datetime] | None = None) -> None:
        super().__init__()
        self._frame_topics: list[str] = ["camera.default.frame"]
        self._detection_topic = "process.motion.detected"
        self._output_topic = "event.snapshot.ready"
        self._output_dir = Path("recordings") / "snapshots"
        self._frame_cache: dict[str, Frame] = {}
        self._subscriptions: list[Subscription] = []
        self._clock = clock or (lambda: dt.datetime.now(tz=dt.UTC))
        self._extension = ".png"
        self._max_snapshots: int | None = None
        self._write_lock = asyncio.Lock()
        self._max_dimension: int | None = None

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        frame_topics = options.get("frame_topics")
        if frame_topics:
            self._frame_topics = list(frame_topics)
        self._detection_topic = options.get("detection_topic", self._detection_topic)
        self._output_topic = options.get("output_topic", self._output_topic)
        self._output_dir = Path(options.get("output_dir", self._output_dir))
        extension = options.get("extension", self._extension)
        self._extension = extension if extension.startswith(".") else f".{extension}"
        max_snapshots = options.get("max_snapshots")
        self._max_snapshots = int(max_snapshots) if max_snapshots is not None else None
        if "max_dimension" in options and options.get("max_dimension") is not None:
            try:
                self._max_dimension = int(options.get("max_dimension"))
            except (TypeError, ValueError):
                self._max_dimension = None

    async def start(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        for topic in self._frame_topics:
            self._subscriptions.append(self.bus.subscribe(topic, self._handle_frame))
        self._subscriptions.append(
            self.bus.subscribe(self._detection_topic, self._handle_detection)
        )
        logger.info(
            "SnapshotWriter watching %d frame topics and persisting to %s",
            len(self._frame_topics),
            self._output_dir,
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()
        logger.info("SnapshotWriter stopped.")

    async def _handle_frame(self, topic: str, payload: Frame) -> None:
        if not isinstance(payload, Frame):
            return
        if payload.image_bytes is None:
            logger.debug(
                "Skipping frame on %s from %s because no image bytes were provided",
                topic,
                payload.camera_id,
            )
            return
        self._frame_cache[payload.camera_id] = payload

    async def _handle_detection(self, topic: str, payload: DetectionEvent) -> None:
        if not isinstance(payload, DetectionEvent):
            return
        if not payload.triggered:
            logger.debug("Ignoring detection on %s because triggered=False", topic)
            return
        frame = self._frame_cache.get(payload.camera_id)
        if frame is None or frame.image_bytes is None:
            logger.warning(
                "No cached frame available for camera %s; skipping snapshot.",
                payload.camera_id,
            )
            return
        async with self._write_lock:
            path = await self._write_snapshot(frame)
            await self._prune_output_dir()
        artifact = SnapshotArtifact(
            camera_id=payload.camera_id,
            artifact_path=str(path),
            content_type=frame.content_type or "application/octet-stream",
            metadata={
                "detection": payload.model_dump(),
                "frame": {
                    "sequence_id": frame.sequence_id,
                    "data_ref": frame.data_ref,
                    "timestamp_utc": frame.timestamp_utc.isoformat(),
                },
            },
        )
        await self.bus.publish(self._output_topic, artifact)
        logger.info(
            "SnapshotWriter persisted snapshot for camera %s at %s",
            payload.camera_id,
            path,
        )

    async def _write_snapshot(self, frame: Frame) -> Path:
        timestamp = self._clock().strftime("%Y%m%d_%H%M%S_%f")
        safe_camera = _CAMERA_SAFE.sub("_", frame.camera_id)
        filename = f"{safe_camera}_{timestamp}{self._extension}"
        path = self._output_dir / filename
        # Decode, optionally resize with aspect ratio, then encode and write
        image = await asyncio.to_thread(self._decode_frame, frame)
        if self._max_dimension and self._max_dimension > 0:
            image = await asyncio.to_thread(self._resize_image, image, self._max_dimension)
        await asyncio.to_thread(self._encode_and_write, image, path, self._extension)
        return path

    def _decode_frame(self, frame: Frame) -> np.ndarray:
        extension = ".png"
        if frame.content_type:
            if "jpeg" in frame.content_type or "jpg" in frame.content_type:
                extension = ".jpg"
            elif "gif" in frame.content_type:
                extension = ".gif"
        with io.BytesIO(frame.image_bytes or b"") as buffer:
            image = iio.imread(buffer, extension=extension)
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        elif image.shape[-1] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
        return image

    def _resize_image(self, image: np.ndarray, max_dimension: int) -> np.ndarray:
        height, width = image.shape[:2]
        if max(height, width) <= max_dimension:
            return image
        scale = max_dimension / max(height, width)
        new_width = int(width * scale)
        new_height = int(height * scale)
        return cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_AREA)

    def _encode_and_write(self, image: np.ndarray, path: Path, extension: str) -> None:
        ext = extension.lower()
        if ext in (".jpg", ".jpeg"):
            iio.imwrite(path, image, extension=".jpg", quality=90)
        elif ext == ".png":
            iio.imwrite(path, image, extension=".png")
        else:
            iio.imwrite(path, image)

    async def _prune_output_dir(self) -> None:
        if not self._max_snapshots or self._max_snapshots <= 0:
            return
        snapshots = sorted(
            self._output_dir.glob(f"*{self._extension}"),
            key=lambda p: p.stat().st_mtime,
        )
        if len(snapshots) <= self._max_snapshots:
            return
        to_delete = len(snapshots) - self._max_snapshots
        for path in snapshots[:to_delete]:
            await asyncio.to_thread(path.unlink)
