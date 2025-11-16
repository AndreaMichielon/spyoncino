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

    def class_names(self) -> Sequence[str]:  # pragma: no cover - protocol
        raise NotImplementedError


class UltralyticsPredictor(PredictorProtocol):
    """Adapter that wraps an Ultralytics YOLO model."""

    def __init__(self, model_path: str | None = None) -> None:
        try:
            from ultralytics import YOLO
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise RuntimeError("Ultralytics is not installed") from exc
        self._model = YOLO(model_path or "yolov8n.pt")
        names = getattr(self._model, "names", {})
        if isinstance(names, dict):
            self._class_names = [str(value) for value in names.values()]
        elif isinstance(names, list | tuple | set):
            self._class_names = [str(value) for value in names]
        else:
            self._class_names = []

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

    def class_names(self) -> Sequence[str]:  # pragma: no cover - simple getter
        return self._class_names


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
        self._alert_labels: set[str] = set()
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
        alert_labels = options.get("alert_labels")
        if alert_labels:
            self._alert_labels = {str(label).lower() for label in alert_labels}
        else:
            self._alert_labels = set()

    async def start(self) -> None:
        if self._predictor is None:
            self._predictor = self._predictor_factory(self._model_path)
        self._validate_alert_labels()
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
        if self._predictor is None:
            logger.warning("YoloDetector has no predictor configured; dropping frame.")
            return
        image_array = await asyncio.to_thread(self._decode_frame_image, payload)
        candidates = await asyncio.to_thread(self._predictor.predict, image_array)
        frame_meta = {
            "width": int(image_array.shape[1]),
            "height": int(image_array.shape[0]),
        }
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
                    "frame": frame_meta,
                },
            )
            await self.bus.publish(self._output_topic, detection)

    def _validate_alert_labels(self) -> None:
        if not self._alert_labels or self._predictor is None:
            return
        try:
            available = {str(name).lower() for name in self._predictor.class_names()}
        except (AttributeError, TypeError):
            available = set()
        missing = [label for label in self._alert_labels if label not in available]
        if missing:
            raise ValueError(
                "Unknown alert labels configured for YoloDetector: "
                f"{', '.join(sorted(missing))}. "
                f"Available classes: {', '.join(sorted(available)) or 'unknown'}"
            )

    def _decode_image(self, image_bytes: bytes, content_type: str | None) -> np.ndarray:
        with io.BytesIO(image_bytes) as buffer:
            extension = ".png"
            if content_type:
                if "jpeg" in content_type or "jpg" in content_type:
                    extension = ".jpg"
                elif "gif" in content_type:
                    extension = ".gif"
            return iio.imread(buffer, extension=extension)

    def _decode_frame_image(self, frame: Frame) -> np.ndarray:
        if frame.data_ref:
            return iio.imread(frame.data_ref)
        if frame.image_bytes is not None:
            return self._decode_image(frame.image_bytes, frame.content_type)
        # As a last resort, return a minimal black image to avoid crashing the pipeline
        return np.zeros((1, 1, 3), dtype=np.uint8)


__all__ = [
    "DetectionCandidate",
    "UltralyticsPredictor",
    "YoloDetector",
]
