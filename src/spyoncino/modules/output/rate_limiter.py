"""
Simple token bucket rate limiter for snapshot notifications.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable

from ...core.bus import Subscription
from ...core.contracts import BaseModule, BasePayload, ModuleConfig, SnapshotArtifact

logger = logging.getLogger(__name__)


class RateLimiter(BaseModule):
    """General-purpose throttler that drops events above a threshold."""

    name = "modules.output.rate_limiter"

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        super().__init__()
        self._input_topic = "event.snapshot.ready"
        self._output_topic = "event.snapshot.allowed"
        self._max_events = 5
        self._per_seconds = 60.0
        self._key_field = "camera_id"
        self._clock = clock or time.monotonic
        self._subscriptions: list[Subscription] = []
        self._history: dict[str, deque[float]] = defaultdict(deque)

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._input_topic = options.get("input_topic", self._input_topic)
        self._output_topic = options.get("output_topic", self._output_topic)
        self._max_events = int(options.get("max_events", self._max_events))
        self._per_seconds = float(options.get("per_seconds", self._per_seconds))
        self._key_field = options.get("key_field", self._key_field)

    async def start(self) -> None:
        self._subscriptions.append(self.bus.subscribe(self._input_topic, self._handle_event))
        logger.info(
            "RateLimiter watching %s with limit %d/%ss",
            self._input_topic,
            self._max_events,
            self._per_seconds,
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()
        self._history.clear()

    async def _handle_event(self, topic: str, payload: BasePayload) -> None:
        key = self._extract_key(payload)
        now = self._clock()
        history = self._history[key]
        self._prune(history, now)
        if len(history) >= self._max_events:
            logger.debug("RateLimiter dropping event for key %s", key)
            return
        history.append(now)
        await self.bus.publish(self._output_topic, payload)

    def _prune(self, history: deque[float], now: float) -> None:
        while history and now - history[0] > self._per_seconds:
            history.popleft()

    def _extract_key(self, payload: BasePayload) -> str:
        if hasattr(payload, self._key_field):
            value = getattr(payload, self._key_field)
            if value is not None:
                return str(value)
        if isinstance(payload, SnapshotArtifact):
            return payload.camera_id
        data = payload.model_dump()
        return str(data.get(self._key_field, "default"))


__all__ = ["RateLimiter"]
