"""
Analytics event logger that bridges bus payloads into the legacy SQLite-backed store.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from ...core.contracts import BaseModule, DetectionEvent, ModuleConfig, StorageStats
from ...legacy.analytics import EventLogger, EventType, SecurityEvent

logger = logging.getLogger(__name__)


class AnalyticsEventLogger(BaseModule):
    """Subscribe to detection/storage topics and persist structured events."""

    name = "modules.analytics.event_logger"

    def __init__(
        self,
        *,
        logger_factory: Callable[..., EventLogger] | None = None,
    ) -> None:
        super().__init__()
        self._logger_factory = logger_factory or self._default_logger_factory
        self._logger: EventLogger | None = None
        self._db_path = Path("recordings") / "events.db"
        self._figure_width = 22.0
        self._figure_height = 5.5
        self._analytics_intervals: list[int] = [5, 15, 60, 120]
        self._detection_topics: list[str] = ["process.motion.unique"]
        self._alert_topics: list[str] = ["process.alert.detected"]
        self._storage_topic = "storage.stats"
        self._subscriptions: list = []

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._db_path = Path(options.get("db_path", self._db_path))
        self._figure_width = float(options.get("figure_width", self._figure_width))
        self._figure_height = float(options.get("figure_height", self._figure_height))
        intervals = options.get("analytics_intervals")
        if intervals:
            self._analytics_intervals = [int(value) for value in intervals]
        detection_topics = options.get("detection_topics")
        if detection_topics:
            self._detection_topics = list(detection_topics)
        alert_topics = options.get("alert_topics")
        if alert_topics:
            self._alert_topics = list(alert_topics)
        self._storage_topic = options.get("storage_topic", self._storage_topic)

    async def start(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = self._logger_factory(
            db_path=str(self._db_path),
            analytics_figure_width=self._figure_width,
            analytics_figure_height=self._figure_height,
            analytics_intervals=self._analytics_intervals,
        )
        for topic in self._unique_topics(self._detection_topics):
            self._subscriptions.append(self.bus.subscribe(topic, self._handle_motion))
        for topic in self._unique_topics(self._alert_topics):
            self._subscriptions.append(self.bus.subscribe(topic, self._handle_alert))
        self._subscriptions.append(self.bus.subscribe(self._storage_topic, self._handle_storage))
        logger.info("AnalyticsEventLogger recording to %s", self._db_path)

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()
        self._logger = None

    async def _handle_motion(self, topic: str, payload: DetectionEvent) -> None:
        if not isinstance(payload, DetectionEvent):
            return
        event = SecurityEvent(
            timestamp=payload.timestamp_utc,
            event_type=EventType.MOTION,
            message=f"Motion detected on {payload.camera_id}",
            metadata=payload.model_dump(),
        )
        await self._log_event(event)

    async def _handle_alert(self, topic: str, payload: DetectionEvent) -> None:
        if not isinstance(payload, DetectionEvent):
            return
        event = SecurityEvent(
            timestamp=payload.timestamp_utc,
            event_type=EventType.PERSON,
            message=f"Person detected on {payload.camera_id}",
            metadata=payload.model_dump(),
        )
        await self._log_event(event)

    async def _handle_storage(self, topic: str, payload: StorageStats) -> None:
        if not isinstance(payload, StorageStats):
            return
        if not payload.warning:
            return
        event = SecurityEvent(
            timestamp=self._logger_clock(),
            event_type=EventType.STORAGE_WARNING,
            message=f"Low storage: {payload.free_gb:.2f}GB free",
            metadata=payload.model_dump(),
            severity="warning",
        )
        await self._log_event(event)

    async def _log_event(self, event: SecurityEvent) -> None:
        if self._logger is None:
            logger.debug("Analytics logger not initialized; dropping event %s", event)
            return
        await asyncio.to_thread(self._logger.log_event, event)

    def _default_logger_factory(self, **kwargs) -> EventLogger:
        return EventLogger(**kwargs)

    def _unique_topics(self, topics: Iterable[str]) -> list[str]:
        unique: list[str] = []
        for topic in topics:
            if topic not in unique:
                unique.append(topic)
        return unique

    def _logger_clock(self) -> dt.datetime:
        return dt.datetime.now(tz=dt.UTC)


__all__ = ["AnalyticsEventLogger"]
