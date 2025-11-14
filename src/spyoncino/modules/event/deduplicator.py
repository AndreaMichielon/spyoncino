"""
Deduplicate high-frequency detection events within a sliding window.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from ...core.bus import Subscription
from ...core.contracts import BaseModule, DetectionEvent, ModuleConfig

logger = logging.getLogger(__name__)


class EventDeduplicator(BaseModule):
    """Suppress duplicate detection events before they fan out downstream."""

    name = "modules.event.deduplicator"

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        super().__init__()
        self._input_topic = "process.motion.detected"
        self._output_topic = "process.motion.unique"
        self._window_seconds = 2.0
        self._key_fields: list[str] = ["camera_id", "detector_id"]
        self._subscriptions: list[Subscription] = []
        self._clock = clock or time.monotonic
        self._history: dict[str, deque[float]] = defaultdict(deque)

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._input_topic = options.get("input_topic", self._input_topic)
        self._output_topic = options.get("output_topic", self._output_topic)
        self._window_seconds = float(options.get("window_seconds", self._window_seconds))
        key_fields = options.get("key_fields")
        if key_fields:
            self._key_fields = list(key_fields)

    async def start(self) -> None:
        self._subscriptions.append(self.bus.subscribe(self._input_topic, self._handle_event))
        logger.info(
            "EventDeduplicator listening on %s and emitting to %s",
            self._input_topic,
            self._output_topic,
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()
        self._history.clear()

    async def _handle_event(self, topic: str, payload: DetectionEvent) -> None:
        if not isinstance(payload, DetectionEvent):
            logger.debug("Ignoring non-detection payload on %s", topic)
            return
        key = self._build_key(payload)
        now = self._clock()
        history = self._history[key]
        self._prune(history, now)
        if history:
            logger.debug(
                "Deduplicating detection for key %s within %.2fs window", key, self._window_seconds
            )
            return
        history.append(now)
        await self.bus.publish(self._output_topic, payload)

    def _prune(self, history: deque[float], now: float) -> None:
        while history and now - history[0] > self._window_seconds:
            history.popleft()

    def _build_key(self, payload: DetectionEvent) -> str:
        data = payload.model_dump()
        parts: list[str] = []
        for path in self._key_fields:
            value = self._lookup(data, path.split("."))
            parts.append(str(value))
        return "|".join(parts)

    def _lookup(self, data: dict[str, Any], segments: list[str]) -> Any:
        current: Any = data
        for segment in segments:
            if isinstance(current, dict):
                current = current.get(segment)
            else:
                current = getattr(current, segment, None)
            if current is None:
                break
        return current


__all__ = ["EventDeduplicator"]
