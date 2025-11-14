"""
YOLO-based object detector module.

The module is intentionally lightweight so it can operate with either the real
Ultralytics model or a stubbed predictor during unit tests.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import imageio.v3 as iio
import numpy as np

from ...core.bus import Subscription
from ...core.contracts import BaseModule, DetectionEvent, Frame, ModuleConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectionCandidate:
    """Represents a single detection result from the predictor."""

    label: str
    confidence: float
    bbox: tuple[float, float, float, float]


class PredictorProtocol:
    """Small protocol so we can swap predictor implementations."""

    def predict(self, image: np.ndarray) -> list[DetectionCandidate]:  # pragma: no cover - protocol
        raise NotImplementedError


class UltralyticsPredictor(PredictorProtocol):
    """Adapter that wraps an Ultralytics YOLO model."""

    def __init__(self, model_path: str | None = None) -> None:
        try:
            from ultralytics import YOLO
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise RuntimeError("Ultralytics is not installed") from exc
        self._model = YOLO(model_path or "yolov8n.pt")

    def predict(self, image: np.ndarray) -> list[DetectionCandidate]:  # pragma: no cover - heavy
        results = self._model(
            image,
            verbose=False,
        )
        candidates: list[DetectionCandidate] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            names = getattr(result, "names", {})
            if boxes is None:
                continue
            for xyxy, cls_id, conf in zip(boxes.xyxy, boxes.cls, boxes.conf, strict=False):
                label = names.get(int(cls_id), str(int(cls_id)))
                bbox = tuple(float(value) for value in xyxy.tolist())  # type: ignore[assignment]
                candidates.append(
                    DetectionCandidate(label=label, confidence=float(conf), bbox=bbox)  # type: ignore[arg-type]
                )
        return candidates


class YoloDetector(BaseModule):
    """Processing module backed by a YOLO model."""

    name = "modules.process.yolo_detector"

    def __init__(
        self,
        *,
        predictor_factory: Callable[[str | None], PredictorProtocol] | None = None,
    ) -> None:
        super().__init__()
        self._predictor_factory = predictor_factory or UltralyticsPredictor
        self._predictor: PredictorProtocol | None = None
        self._input_topics: list[str] = ["camera.default.frame"]
        self._output_topic = "process.yolo.detected"
        self._detector_id = "yolo-v8"
        self._model_path: str | None = None
        self._confidence_threshold = 0.25
        self._class_filter: set[str] = set()
        self._subscriptions: list[Subscription] = []

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        input_topics: Sequence[str] | None = options.get("input_topics")
        if input_topics:
            self._input_topics = list(input_topics)
        self._output_topic = options.get("output_topic", self._output_topic)
        self._detector_id = options.get("detector_id", self._detector_id)
        self._model_path = options.get("model_path", self._model_path)
        self._confidence_threshold = float(
            options.get("confidence_threshold", self._confidence_threshold)
        )
        class_filter = options.get("class_filter")
        if class_filter:
            self._class_filter = {str(item) for item in class_filter}

    async def start(self) -> None:
        if self._predictor is None:
            self._predictor = self._predictor_factory(self._model_path)
        for topic in self._input_topics:
            self._subscriptions.append(self.bus.subscribe(topic, self._handle_frame))
        logger.info(
            "YoloDetector %s watching %d topics with threshold %.2f",
            self._detector_id,
            len(self._input_topics),
            self._confidence_threshold,
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()

    async def _handle_frame(self, topic: str, payload: Frame) -> None:
        if not isinstance(payload, Frame):
            logger.debug("YoloDetector received unsupported payload on %s", topic)
            return
        if payload.image_bytes is None:
            logger.debug(
                "Skipping frame %s because no image bytes were provided.", payload.data_ref
            )
            return
        if self._predictor is None:
            logger.warning("YoloDetector has no predictor configured; dropping frame.")
            return
        image_array = await asyncio.to_thread(
            self._decode_image, payload.image_bytes, payload.content_type
        )
        candidates = await asyncio.to_thread(self._predictor.predict, image_array)
        for candidate in candidates:
            if candidate.confidence < self._confidence_threshold:
                continue
            if self._class_filter and candidate.label not in self._class_filter:
                continue
            detection = DetectionEvent(
                camera_id=payload.camera_id,
                detector_id=self._detector_id,
                frame_ref=payload.data_ref,
                confidence=candidate.confidence,
                attributes={
                    "label": candidate.label,
                    "bbox": candidate.bbox,
                },
            )
            await self.bus.publish(self._output_topic, detection)

    def _decode_image(self, image_bytes: bytes, content_type: str | None) -> np.ndarray:
        with io.BytesIO(image_bytes) as buffer:
            extension = ".png"
            if content_type:
                if "jpeg" in content_type or "jpg" in content_type:
                    extension = ".jpg"
                elif "gif" in content_type:
                    extension = ".gif"
            return iio.imread(buffer, extension=extension)


__all__ = [
    "DetectionCandidate",
    "UltralyticsPredictor",
    "YoloDetector",
]
