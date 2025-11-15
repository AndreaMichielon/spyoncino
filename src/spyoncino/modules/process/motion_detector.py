"""
Minimal motion detector module for exercising the new event bus.

The implementation simply receives frames from the configured topic and
emits a `DetectionEvent` to demonstrate the processing flow.
"""

from __future__ import annotations

import logging

from ...core.bus import Subscription
from ...core.contracts import BaseModule, DetectionEvent, Frame, ModuleConfig

logger = logging.getLogger(__name__)


class MotionDetector(BaseModule):
    """Baseline motion detector that emits synthetic events."""

    name = "modules.process.motion_detector"

    def __init__(self, *, detector_id: str = "motion-basic") -> None:
        super().__init__()
        self._detector_id = detector_id
        self._input_topics: list[str] = ["camera.default.frame"]
        self._subscriptions: list[Subscription] = []

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._detector_id = options.get("detector_id", self._detector_id)
        topics = options.get("input_topics")
        if isinstance(topics, list | tuple | set):
            self._input_topics = [str(topic) for topic in topics]
        else:
            self._input_topics = [options.get("input_topic", self._input_topics[0])]

    async def start(self) -> None:
        async def _handle_frame(topic: str, payload: Frame) -> None:
            if not isinstance(payload, Frame):
                logger.debug("Ignoring payload on %s that is not a Frame: %s", topic, type(payload))
                return
            detection = DetectionEvent(
                camera_id=payload.camera_id,
                detector_id=self._detector_id,
                frame_ref=payload.data_ref,
                attributes={
                    "sequence_id": payload.sequence_id,
                },
            )
            await self.bus.publish("process.motion.detected", detection)
            logger.info("Published synthetic detection for camera %s", payload.camera_id)

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
