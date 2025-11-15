"""
Router module that filters YOLO detections for alert-worthy events with anti-spam guards.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from ...core.bus import Subscription
from ...core.contracts import BaseModule, DetectionEvent, ModuleConfig

logger = logging.getLogger(__name__)


class DetectionEventRouter(BaseModule):
    """Emit `process.alert.detected` events when configured labels match."""

    name = "modules.process.detection_event_router"

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        super().__init__()
        self._input_topic = "process.yolo.detected"
        self._output_topic = "process.alert.detected"
        self._target_labels: set[str] = {"person"}
        self._min_confidence = 0.35
        self._cooldown_seconds = 30.0
        self._bbox_iou_threshold = 0.6
        self._timeout_seconds = 5.0
        self._subscription: Subscription | None = None
        self._clock = clock or time.monotonic
        self._last_detections: dict[
            str, tuple[float, tuple[float, float, float, float] | None]
        ] = {}

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._input_topic = options.get("input_topic", self._input_topic)
        self._output_topic = options.get("output_topic", self._output_topic)
        labels = options.get("target_labels")
        if labels:
            self._target_labels = {str(label).lower() for label in labels}
        self._min_confidence = float(options.get("min_confidence", self._min_confidence))
        self._cooldown_seconds = float(options.get("cooldown_seconds", self._cooldown_seconds))
        self._bbox_iou_threshold = float(
            options.get("bbox_iou_threshold", self._bbox_iou_threshold)
        )
        self._timeout_seconds = float(options.get("timeout_seconds", self._timeout_seconds))

    async def start(self) -> None:
        self._subscription = self.bus.subscribe(self._input_topic, self._handle_detection)
        logger.info(
            "DetectionEventRouter listening on %s and emitting to %s",
            self._input_topic,
            self._output_topic,
        )

    async def stop(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription)
            self._subscription = None
        self._last_detections.clear()

    async def _handle_detection(self, topic: str, payload: DetectionEvent) -> None:
        if not isinstance(payload, DetectionEvent):
            return
        if payload.confidence < self._min_confidence:
            return
        label = str(payload.attributes.get("label", "")).lower()
        if self._target_labels and label not in self._target_labels:
            return
        bbox = self._bbox_from_attributes(payload.attributes)
        if self._should_suppress(payload.camera_id, bbox):
            logger.debug(
                "Suppressing alert detection on %s due to cooldown/overlap.",
                payload.camera_id,
            )
            return
        self._update_last_detection(payload.camera_id, bbox)

        enriched = DetectionEvent(
            camera_id=payload.camera_id,
            detector_id=f"{payload.detector_id}.alert-router",
            timestamp_utc=payload.timestamp_utc,
            frame_ref=payload.frame_ref,
            confidence=payload.confidence,
            attributes={
                **payload.attributes,
                "source_detector": payload.detector_id,
                "router": {
                    "cooldown_seconds": self._cooldown_seconds,
                    "bbox_iou_threshold": self._bbox_iou_threshold,
                },
            },
        )
        await self.bus.publish(self._output_topic, enriched)
        logger.info("DetectionEventRouter emitted detection for camera %s", payload.camera_id)

    def _bbox_from_attributes(
        self, attributes: dict[str, Any]
    ) -> tuple[float, float, float, float] | None:
        bbox = attributes.get("bbox")
        if not bbox:
            return None
        try:
            x1, y1, x2, y2 = (float(value) for value in bbox)
        except (TypeError, ValueError):
            return None
        return (x1, y1, x2, y2)

    def _should_suppress(
        self, camera_id: str, bbox: tuple[float, float, float, float] | None
    ) -> bool:
        last = self._last_detections.get(camera_id)
        if not last:
            return False
        last_time, last_bbox = last
        age = self._clock() - last_time
        if age > self._timeout_seconds:
            return False
        if age < self._cooldown_seconds:
            return True
        if bbox and last_bbox:
            overlap = self._iou(bbox, last_bbox)
            if overlap >= self._bbox_iou_threshold:
                return True
        return False

    def _update_last_detection(
        self, camera_id: str, bbox: tuple[float, float, float, float] | None
    ) -> None:
        self._last_detections[camera_id] = (self._clock(), bbox)

    def _iou(
        self,
        bbox_a: tuple[float, float, float, float],
        bbox_b: tuple[float, float, float, float],
    ) -> float:
        x1 = max(bbox_a[0], bbox_b[0])
        y1 = max(bbox_a[1], bbox_b[1])
        x2 = min(bbox_a[2], bbox_b[2])
        y2 = min(bbox_a[3], bbox_b[3])
        if x2 <= x1 or y2 <= y1:
            return 0.0
        intersection = (x2 - x1) * (y2 - y1)
        area_a = (bbox_a[2] - bbox_a[0]) * (bbox_a[3] - bbox_a[1])
        area_b = (bbox_b[2] - bbox_b[0]) * (bbox_b[3] - bbox_b[1])
        union = area_a + area_b - intersection
        if union <= 0:
            return 0.0
        return intersection / union


__all__ = ["DetectionEventRouter"]
