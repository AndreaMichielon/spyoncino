"""GIF builder that stitches buffered frames into short animations."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from collections import defaultdict, deque
from pathlib import Path

import imageio.v3 as iio
import numpy as np

from ...core.bus import Subscription
from ...core.contracts import BaseModule, DetectionEvent, Frame, ModuleConfig, SnapshotArtifact

logger = logging.getLogger(__name__)
_SAFE_CAMERA = re.compile(r"[^A-Za-z0-9_-]")


class GifBuilder(BaseModule):
    """Build GIF artifacts from buffered frames whenever detections fire."""

    name = "modules.event.gif_builder"

    def __init__(self) -> None:
        super().__init__()
        self._frame_topics: list[str] = ["camera.default.frame"]
        self._detection_topic = "process.yolo.detected"
        self._output_topic = "event.gif.ready"
        self._output_dir = Path("recordings") / "gifs"
        self._frames: dict[str, deque[Frame]] = defaultdict(lambda: deque(maxlen=90))
        self._max_frames = 90
        self._fps = 10
        self._duration_seconds = 3
        self._max_artifacts: int | None = 50
        self._subscriptions: list[Subscription] = []
        self._write_lock = asyncio.Lock()

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        frame_topics = options.get("frame_topics")
        if frame_topics:
            self._frame_topics = list(frame_topics)
        self._detection_topic = options.get("detection_topic", self._detection_topic)
        self._output_topic = options.get("output_topic", self._output_topic)
        self._output_dir = Path(options.get("output_dir", self._output_dir))
        self._max_frames = int(options.get("max_frames", self._max_frames))
        self._fps = int(options.get("fps", self._fps))
        self._duration_seconds = float(options.get("duration_seconds", self._duration_seconds))
        max_artifacts = options.get("max_artifacts")
        self._max_artifacts = int(max_artifacts) if max_artifacts is not None else None
        # resize deque maxlen for all existing cameras
        for camera_id, buffer in self._frames.items():
            self._frames[camera_id] = deque(buffer, maxlen=self._max_frames)

    async def start(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        for topic in self._frame_topics:
            self._subscriptions.append(self.bus.subscribe(topic, self._handle_frame))
        self._subscriptions.append(
            self.bus.subscribe(self._detection_topic, self._handle_detection)
        )
        logger.info(
            "GifBuilder tracking %d frame topics; writing to %s",
            len(self._frame_topics),
            self._output_dir,
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()

    async def _handle_frame(self, topic: str, payload: Frame) -> None:
        if not isinstance(payload, Frame):
            return
        if payload.image_bytes is None:
            return
        buffer = self._frames[payload.camera_id]
        buffer.append(payload)

    async def _handle_detection(self, topic: str, payload: DetectionEvent) -> None:
        if not isinstance(payload, DetectionEvent):
            return
        frames = list(self._frames.get(payload.camera_id, ()))
        if not frames:
            logger.debug("No frames buffered for camera %s; skipping GIF.", payload.camera_id)
            return
        target_frames = min(
            len(frames), max(1, int(self._fps * self._duration_seconds)), self._max_frames
        )
        selected = frames[-target_frames:]
        async with self._write_lock:
            path = await self._write_gif(payload.camera_id, selected)
            await self._prune_output_dir()
        artifact = SnapshotArtifact(
            camera_id=payload.camera_id,
            artifact_path=str(path),
            content_type="image/gif",
            metadata={
                "detection": payload.model_dump(),
                "frame_count": len(selected),
            },
        )
        await self.bus.publish(self._output_topic, artifact)
        logger.info("GifBuilder persisted %s for camera %s", path.name, payload.camera_id)

    async def _write_gif(self, camera_id: str, frames: list[Frame]) -> Path:
        timestamp = frames[-1].timestamp_utc.strftime("%Y%m%d_%H%M%S_%f")
        safe_camera = _SAFE_CAMERA.sub("_", camera_id)
        filename = f"{safe_camera}_{timestamp}.gif"
        path = self._output_dir / filename
        images: list[np.ndarray] = []
        for frame in frames:
            if frame.image_bytes is None:
                continue
            extension = ".png"
            if frame.content_type and "jpeg" in frame.content_type:
                extension = ".jpg"
            with io.BytesIO(frame.image_bytes) as buffer:
                images.append(iio.imread(buffer, extension=extension))
        if not images:
            raise RuntimeError("Failed to decode frames for GIF generation.")
        frame_duration = int(1000 / max(1, self._fps))
        await asyncio.to_thread(
            iio.imwrite,
            path,
            images,
            extension=".gif",
            loop=0,
            duration=frame_duration,
        )
        return path

    async def _prune_output_dir(self) -> None:
        if not self._max_artifacts or self._max_artifacts <= 0:
            return
        artifacts = sorted(self._output_dir.glob("*.gif"), key=lambda item: item.stat().st_mtime)
        if len(artifacts) <= self._max_artifacts:
            return
        for path in artifacts[: len(artifacts) - self._max_artifacts]:
            await asyncio.to_thread(path.unlink)


__all__ = ["GifBuilder"]
