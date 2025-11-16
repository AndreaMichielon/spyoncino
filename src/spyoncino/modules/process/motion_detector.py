"""
Basic motion detector with frame differencing and interval-based evaluation.

The module buffers the previous grayscale frame per camera and, at a configured
interval, computes a simple absolute difference to estimate motion. A detection
is emitted only when the motion percentage crosses the configured threshold.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from dataclasses import dataclass

import imageio.v3 as iio
import numpy as np

from ...core.bus import Subscription
from ...core.contracts import BaseModule, DetectionEvent, Frame, ModuleConfig

logger = logging.getLogger(__name__)


@dataclass
class _CameraState:
    last_gray: np.ndarray | None = None
    last_eval_ts: float = 0.0


class MotionDetector(BaseModule):
    """Simple motion detector using frame differencing and thresholding."""

    name = "modules.process.motion_detector"

    def __init__(self, *, detector_id: str = "motion-basic") -> None:
        super().__init__()
        self._detector_id = detector_id
        self._input_topics: list[str] = ["camera.default.frame"]
        self._subscriptions: list[Subscription] = []
        self._interval_seconds: float = 0.0
        self._motion_threshold_percent: int = 5
        self._states: dict[str, _CameraState] = {}

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._detector_id = options.get("detector_id", self._detector_id)
        topics = options.get("input_topics")
        if isinstance(topics, list | tuple | set):
            self._input_topics = [str(topic) for topic in topics]
        else:
            self._input_topics = [options.get("input_topic", self._input_topics[0])]
        if "interval_seconds" in options:
            with contextlib.suppress(TypeError, ValueError):
                self._interval_seconds = float(options["interval_seconds"])
        if "motion_threshold" in options:
            with contextlib.suppress(TypeError, ValueError):
                self._motion_threshold_percent = int(options["motion_threshold"])

    async def start(self) -> None:
        async def _handle_frame(topic: str, payload: Frame) -> None:
            if not isinstance(payload, Frame):
                logger.debug("Ignoring payload on %s that is not a Frame: %s", topic, type(payload))
                return
            if payload.image_bytes is None:
                # No pixels to evaluate; skip gracefully.
                return

            state = self._states.setdefault(payload.camera_id, _CameraState())
            now = time.monotonic()
            # Allow the first evaluation immediately even if interval > 0
            if state.last_eval_ts != 0.0 and now - state.last_eval_ts < max(
                0.0, self._interval_seconds
            ):
                # Throttle evaluations to the configured cadence.
                return

            gray = await asyncio.to_thread(
                self._decode_to_gray, payload.image_bytes, payload.content_type
            )

            motion_percent = 0
            if state.last_gray is not None and state.last_gray.shape == gray.shape:
                diff = cv2_absdiff(state.last_gray, gray)
                # Light blur helps suppress noise
                blurred = gaussian_blur(diff, ksize=5)
                thresh = threshold_binary(blurred, thresh=25)
                motion_pixels = int(thresh.sum() // 255)
                total_pixels = int(thresh.size)
                motion_percent = int((motion_pixels / max(1, total_pixels)) * 100)
                state.last_eval_ts = now
            else:
                # Establish baseline without emitting; do not advance eval timer
                pass

            state.last_gray = gray

            if motion_percent >= self._motion_threshold_percent:
                detection = DetectionEvent(
                    camera_id=payload.camera_id,
                    detector_id=self._detector_id,
                    frame_ref=payload.data_ref,
                    attributes={
                        "sequence_id": payload.sequence_id,
                        "motion_percent": motion_percent,
                    },
                )
                await self.bus.publish("process.motion.detected", detection)
                logger.info(
                    "MotionDetector detected motion=%d%% on camera %s",
                    motion_percent,
                    payload.camera_id,
                )

        for topic in self._input_topics:
            subscription = self.bus.subscribe(topic, _handle_frame)
            self._subscriptions.append(subscription)
            logger.info("MotionDetector %s subscribed to %s", self._detector_id, topic)

    async def stop(self) -> None:
        while self._subscriptions:
            subscription = self._subscriptions.pop()
            self.bus.unsubscribe(subscription)
            logger.info(
                "MotionDetector %s unsubscribed from %s", self._detector_id, subscription.topic
            )

    def _decode_to_gray(self, image_bytes: bytes, content_type: str | None) -> np.ndarray:
        extension = ".png"
        if content_type:
            ct = content_type.lower()
            if "jpeg" in ct or "jpg" in ct:
                extension = ".jpg"
            elif "gif" in ct:
                extension = ".gif"
        with io.BytesIO(image_bytes) as buffer:
            image = iio.imread(buffer, extension=extension)
        if image.ndim == 2:
            gray = image.astype(np.uint8, copy=False)
        else:
            # Convert RGB/RGBA to grayscale using luminosity method
            if image.shape[-1] == 4:
                image = image[..., :3]
            # weights: 0.299 R, 0.587 G, 0.114 B
            gray = np.dot(image[..., :3], np.array([0.299, 0.587, 0.114], dtype=np.float32))
            gray = np.clip(gray, 0, 255).astype(np.uint8)
        return gray


def cv2_absdiff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    # Fast absolute difference equivalent without OpenCV dependency
    return np.abs(a.astype(np.int16) - b.astype(np.int16)).astype(np.uint8)


def gaussian_blur(gray: np.ndarray, ksize: int = 5) -> np.ndarray:
    # Simple separable Gaussian approximation; for small k it's fine
    # Use a fixed kernel approximating (1,4,6,4,1)/16 when ksize=5
    if ksize != 5:
        return gray  # keep simple; can extend if needed
    kernel = np.array([1, 4, 6, 4, 1], dtype=np.float32)
    kernel /= kernel.sum()
    tmp = convolve1d(gray, kernel, axis=0)
    blurred = convolve1d(tmp, kernel, axis=1)
    return blurred.astype(np.uint8, copy=False)


def threshold_binary(gray: np.ndarray, thresh: int) -> np.ndarray:
    return (gray > thresh).astype(np.uint8) * 255


def convolve1d(arr: np.ndarray, kernel: np.ndarray, axis: int) -> np.ndarray:
    pad = (len(kernel) - 1) // 2
    arr_padded = np.pad(
        arr, [(pad, pad) if i == axis else (0, 0) for i in range(arr.ndim)], mode="edge"
    )
    # Move axis to last for simplicity
    arr_swapped = np.moveaxis(arr_padded, axis, -1)
    # Output should have the original length along the convolved axis
    original_length = arr.shape[axis]
    out_shape = arr_swapped.shape[:-1] + (original_length,)
    out = np.zeros(out_shape, dtype=np.float32)
    # Convolve along last axis
    for i in range(len(kernel)):
        out += kernel[i] * arr_swapped[..., i : i + original_length]
    # Move axis back
    out = np.moveaxis(out, -1, axis)
    return out
