"""GIF builder that stitches buffered frames into short animations."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import cv2
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
        # Buffer size will be derived from fps * duration_seconds
        self._frames: dict[str, deque[Frame]] = defaultdict(lambda: deque(maxlen=30))
        self._fps = 10
        self._duration_seconds = 3
        self._max_artifacts: int | None = 50
        self._apply_overlays = True
        self._max_dimension = 640
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
        self._fps = int(options.get("fps", self._fps))
        self._duration_seconds = float(options.get("duration_seconds", self._duration_seconds))
        self._max_dimension = int(options.get("max_dimension", self._max_dimension))
        max_artifacts = options.get("max_artifacts")
        self._max_artifacts = int(max_artifacts) if max_artifacts is not None else None
        if "apply_overlays" in options:
            self._apply_overlays = bool(options.get("apply_overlays"))
        # resize deque maxlen for all existing cameras based on fps * duration
        for camera_id, buffer in self._frames.items():
            target_len = max(1, int(self._fps * self._duration_seconds))
            self._frames[camera_id] = deque(buffer, maxlen=target_len)
        # ensure new camera buffers also use the derived length
        target_len = max(1, int(self._fps * self._duration_seconds))
        self._frames.default_factory = lambda: deque(maxlen=target_len)

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
        buffer = self._frames[payload.camera_id]
        buffer.append(payload)

    async def _handle_detection(self, topic: str, payload: DetectionEvent) -> None:
        if not isinstance(payload, DetectionEvent):
            return
        frames = list(self._frames.get(payload.camera_id, ()))
        if not frames:
            logger.debug("No frames buffered for camera %s; skipping GIF.", payload.camera_id)
            return
        target_frames = min(len(frames), max(1, int(self._fps * self._duration_seconds)))
        all_selected = frames[-target_frames:] if len(frames) > target_frames else frames
        timestamp = all_selected[-1].timestamp_utc
        processed_frames = await asyncio.to_thread(self._prepare_frames, all_selected, payload)
        async with self._write_lock:
            path = await self._write_gif(payload.camera_id, processed_frames, timestamp)
            await self._prune_output_dir()
        artifact = SnapshotArtifact(
            camera_id=payload.camera_id,
            artifact_path=str(path),
            content_type="image/gif",
            metadata={
                "detection": payload.model_dump(),
                "frame_count": len(processed_frames),
            },
        )
        await self.bus.publish(self._output_topic, artifact)
        logger.info("GifBuilder persisted %s for camera %s", path.name, payload.camera_id)

    async def _write_gif(
        self, camera_id: str, images: list[np.ndarray], timestamp: dt.datetime
    ) -> Path:
        timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S_%f")
        safe_camera = _SAFE_CAMERA.sub("_", camera_id)
        filename = f"{safe_camera}_{timestamp_str}.gif"
        path = self._output_dir / filename
        if not images:
            raise RuntimeError("No frames provided for GIF generation.")
        frame_duration = int(1000 / max(1, self._fps))
        await asyncio.to_thread(
            iio.imwrite,
            path,
            images,
            extension=".gif",
            loop=0,
            duration=frame_duration,
            quantizer="nq",
        )
        return path

    def _prepare_frames(self, frames: list[Frame], detection: DetectionEvent) -> list[np.ndarray]:
        prepared: list[np.ndarray] = []
        overlay_mode = self._overlay_mode(detection)
        boxes = self._extract_boxes(detection.attributes)
        prev_gray: np.ndarray | None = None

        for frame in frames:
            if frame.image_bytes is None:
                continue
            image = self._decode_frame(frame)
            if self._apply_overlays:
                if overlay_mode == "detection" and boxes:
                    image = self._draw_detection_overlay(image, detection, boxes)
                elif overlay_mode == "motion":
                    image, prev_gray = self._draw_motion_overlay(image, prev_gray)
            prepared.append(image)

        if not prepared:
            raise RuntimeError("Failed to decode frames for GIF generation.")

        # Resize frames (selection already handled by target_frames upstream)
        resized_frames = [self._resize_for_gif(frame) for frame in prepared]
        return resized_frames

    def _decode_frame(self, frame: Frame) -> np.ndarray:
        if frame.data_ref:
            image = iio.imread(frame.data_ref)
        else:
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

    def _overlay_mode(self, detection: DetectionEvent) -> str:
        attributes = detection.attributes or {}
        if attributes.get("bbox"):
            return "detection"
        nested = attributes.get("detections")
        if isinstance(nested, list):
            for entry in nested:
                if isinstance(entry, dict) and entry.get("bbox"):
                    return "detection"
        return "motion"

    def _extract_boxes(self, attributes: dict[str, Any]) -> list[dict[str, Any]]:
        boxes: list[dict[str, Any]] = []
        bbox = self._coerce_bbox(attributes.get("bbox"))
        if bbox:
            boxes.append(
                {
                    "bbox": bbox,
                    "label": attributes.get("label"),
                    "confidence": attributes.get("confidence"),
                }
            )
        detections = attributes.get("detections") or []
        if isinstance(detections, list):
            for entry in detections:
                if not isinstance(entry, dict):
                    continue
                entry_bbox = self._coerce_bbox(entry.get("bbox"))
                if not entry_bbox:
                    continue
                boxes.append(
                    {
                        "bbox": entry_bbox,
                        "label": entry.get("label"),
                        "confidence": entry.get("confidence"),
                    }
                )
        return boxes

    def _draw_detection_overlay(
        self, image: np.ndarray, detection: DetectionEvent, boxes: list[dict[str, Any]]
    ) -> np.ndarray:
        if not boxes:
            return image
        bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        height, width = bgr.shape[:2]
        for box in boxes:
            coords = self._sanitize_bbox(box["bbox"], width, height)
            color = (0, 0, 255)
            cv2.rectangle(bgr, (coords[0], coords[1]), (coords[2], coords[3]), color, 2)
            label = box.get("label") or detection.attributes.get("label") or "object"
            confidence = box.get("confidence", detection.confidence)
            caption = f"{label} {confidence:.2f}" if confidence is not None else label
            cv2.putText(
                bgr,
                caption,
                (coords[0], max(15, coords[1] - 5)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                cv2.LINE_AA,
            )
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    def _draw_motion_overlay(
        self, image: np.ndarray, prev_gray: np.ndarray | None
    ) -> tuple[np.ndarray, np.ndarray]:
        bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        motion_percent = 0
        if prev_gray is not None:
            diff = cv2.absdiff(prev_gray, gray)
            blurred = cv2.GaussianBlur(diff, (5, 5), 0)
            _, thresh = cv2.threshold(blurred, 25, 255, cv2.THRESH_BINARY)
            motion_pixels = cv2.countNonZero(thresh)
            total_pixels = thresh.size
            motion_percent = int((motion_pixels / max(1, total_pixels)) * 100)
            heatmap = cv2.applyColorMap(thresh, cv2.COLORMAP_JET)
            bgr = cv2.addWeighted(bgr, 0.8, heatmap, 0.2, 0)
        self._draw_motion_hud(bgr, motion_percent)
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), gray

    def _draw_motion_hud(self, frame: np.ndarray, motion_percent: int) -> None:
        height, width = frame.shape[:2]
        margin = max(10, width // 40)
        panel_width = max(180, width // 4)
        panel_height = max(60, height // 8)
        top_left = (margin, margin)
        bottom_right = (margin + panel_width, margin + panel_height)
        overlay = frame.copy()
        cv2.rectangle(overlay, top_left, bottom_right, (30, 30, 30), -1)
        cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
        cv2.rectangle(frame, top_left, bottom_right, (0, 165, 255), 1)
        status = "MOTION DETECTED" if motion_percent > 0 else "MONITORING"
        cv2.putText(
            frame,
            status,
            (margin + 10, margin + 22),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 255, 180),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            f"Motion: {motion_percent}%",
            (margin + 10, margin + 42),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

    def _sanitize_bbox(
        self, bbox: tuple[float, float, float, float], width: int, height: int
    ) -> tuple[int, int, int, int]:
        x1 = max(0, min(width - 1, int(round(bbox[0]))))
        y1 = max(0, min(height - 1, int(round(bbox[1]))))
        x2 = max(0, min(width - 1, int(round(bbox[2]))))
        y2 = max(0, min(height - 1, int(round(bbox[3]))))
        if x2 <= x1:
            x2 = min(width - 1, x1 + 1)
        if y2 <= y1:
            y2 = min(height - 1, y1 + 1)
        return x1, y1, x2, y2

    @staticmethod
    def _coerce_bbox(bbox: Any) -> tuple[float, float, float, float] | None:
        if bbox is None:
            return None
        try:
            x1, y1, x2, y2 = (float(value) for value in bbox)
            return x1, y1, x2, y2
        except (TypeError, ValueError):
            return None

    # Note: key-frame selection removed; fps x duration defines frame count upstream.

    def _resize_for_gif(self, frame: np.ndarray) -> np.ndarray:
        """Resize frame with max side from config, preserving aspect ratio and even dimensions."""
        height, width = frame.shape[:2]
        max_dimension = self._max_dimension

        # Skip if already within bounds
        if max(height, width) <= max_dimension:
            # Still ensure even dimensions for optimal compression
            if width % 2 != 0 or height % 2 != 0:
                new_width = width + (width % 2)
                new_height = height + (height % 2)
                return cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
            return frame

        # Calculate scale factor based on largest dimension
        scale = max_dimension / max(height, width)
        new_width = int(width * scale)
        new_height = int(height * scale)

        # Ensure even dimensions for optimal compression
        new_width += new_width % 2
        new_height += new_height % 2

        return cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)

    async def _prune_output_dir(self) -> None:
        if not self._max_artifacts or self._max_artifacts <= 0:
            return
        artifacts = sorted(self._output_dir.glob("*.gif"), key=lambda item: item.stat().st_mtime)
        if len(artifacts) <= self._max_artifacts:
            return
        for path in artifacts[: len(artifacts) - self._max_artifacts]:
            await asyncio.to_thread(path.unlink)


__all__ = ["GifBuilder"]
