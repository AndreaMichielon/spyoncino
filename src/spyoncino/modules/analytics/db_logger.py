"""
Analytics database logger that persists bus events using SQLModel/SQLAlchemy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import Callable, Iterable
from typing import Any

from sqlmodel import Field, Session, SQLModel, select
from sqlmodel import create_engine as sqlmodel_create_engine

from ...core.contracts import AnalyticsCursor, BaseModule, BasePayload, HealthStatus, ModuleConfig

logger = logging.getLogger(__name__)


class AnalyticsEvent(SQLModel, table=True):
    """SQLModel table storing raw bus events for analytics dashboards."""

    id: int | None = Field(default=None, primary_key=True)
    topic: str = Field(index=True)
    payload_type: str = Field(index=True)
    event_timestamp: dt.datetime = Field(index=True)
    payload_json: str
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(tz=dt.UTC))


class AnalyticsDbLogger(BaseModule):
    """Persist bus events into SQL databases and publish cursor telemetry."""

    name = "modules.analytics.db_logger"

    def __init__(
        self,
        *,
        engine_factory: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__()
        self._engine_factory = engine_factory or self._default_engine_factory
        self._engine: Any | None = None
        self._database_url = "sqlite:///recordings/events.db"
        self._topics: list[str] = [
            "process.motion.unique",
            "process.alert.detected",
            "storage.stats",
        ]
        self._cursor_topic = "analytics.persistence.cursor"
        self._backfill_history_seconds = 0
        self._subscriptions = []
        self._last_cursor: AnalyticsCursor | None = None

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._database_url = options.get("database_url", self._database_url)
        topics = options.get("topics")
        if isinstance(topics, Iterable):
            self._topics = list(dict.fromkeys(str(topic) for topic in topics))
        self._cursor_topic = options.get("cursor_topic", self._cursor_topic)
        self._backfill_history_seconds = int(
            options.get("backfill_history_seconds", self._backfill_history_seconds)
        )

    async def start(self) -> None:
        self._engine = self._engine_factory(self._database_url)
        SQLModel.metadata.create_all(self._engine)
        for topic in self._topics:
            self._subscriptions.append(self.bus.subscribe(topic, self._handle_event))
        logger.info("AnalyticsDbLogger subscribed to %d topics.", len(self._topics))
        await self._publish_existing_cursor()

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()
        self._engine = None

    async def health(self) -> HealthStatus:
        status = "healthy" if self._engine else "degraded"
        details = {"database_url": self._database_url, "last_cursor": None}
        if self._last_cursor:
            details["last_cursor"] = self._last_cursor.model_dump(mode="python")
        return HealthStatus(status=status, details=details)

    async def _handle_event(self, topic: str, payload: BasePayload) -> None:
        if self._engine is None:
            logger.debug("Analytics DB engine unavailable; dropping payload from %s", topic)
            return
        timestamp = self._extract_timestamp(payload)
        payload_dict = payload.model_dump(mode="json")
        payload_json = json.dumps(payload_dict, default=str)
        payload_type = payload.__class__.__name__
        record_id = await asyncio.to_thread(
            self._insert_event, topic, payload_type, timestamp, payload_json
        )
        lag = max(0.0, (dt.datetime.now(tz=dt.UTC) - timestamp).total_seconds())
        cursor = AnalyticsCursor(
            cursor_id=record_id,
            topic=topic,
            event_timestamp=timestamp,
            lag_seconds=lag,
        )
        self._last_cursor = cursor
        await self.bus.publish(self._cursor_topic, cursor)

    def _insert_event(
        self,
        topic: str,
        payload_type: str,
        timestamp: dt.datetime,
        payload_json: str,
    ) -> int:
        assert self._engine is not None
        with Session(self._engine) as session:
            record = AnalyticsEvent(
                topic=topic,
                payload_type=payload_type,
                event_timestamp=timestamp,
                payload_json=payload_json,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record.id or 0

    def _extract_timestamp(self, payload: BasePayload) -> dt.datetime:
        timestamp = getattr(payload, "timestamp_utc", None) or getattr(payload, "timestamp", None)
        if timestamp is None:
            timestamp = dt.datetime.now(tz=dt.UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.UTC)
        return timestamp

    async def _publish_existing_cursor(self) -> None:
        if self._engine is None:
            return
        last_cursor = await asyncio.to_thread(self._load_last_cursor)
        if last_cursor:
            self._last_cursor = last_cursor
            await self.bus.publish(self._cursor_topic, last_cursor)

    def _load_last_cursor(self) -> AnalyticsCursor | None:
        assert self._engine is not None
        with Session(self._engine) as session:
            stmt = select(AnalyticsEvent).order_by(AnalyticsEvent.id.desc()).limit(1)
            record = session.exec(stmt).first()
            if record is None:
                return None
            lag = max(0.0, (dt.datetime.now(tz=dt.UTC) - record.event_timestamp).total_seconds())
            return AnalyticsCursor(
                cursor_id=record.id or 0,
                topic=record.topic,
                event_timestamp=record.event_timestamp,
                lag_seconds=lag,
            )

    def _default_engine_factory(self, database_url: str):
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        return sqlmodel_create_engine(database_url, echo=False, connect_args=connect_args)


__all__ = ["AnalyticsDbLogger", "AnalyticsEvent"]
