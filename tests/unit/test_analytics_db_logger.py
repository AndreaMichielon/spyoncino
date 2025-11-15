import asyncio
import datetime as dt
from pathlib import Path

import pytest
from sqlmodel import Session, create_engine, select

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import DetectionEvent, ModuleConfig
from spyoncino.modules.analytics.db_logger import AnalyticsDbLogger, AnalyticsEvent


@pytest.mark.asyncio
async def test_db_logger_persists_events(tmp_path: Path) -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    database_url = f"sqlite:///{tmp_path/'events.db'}"
    module = AnalyticsDbLogger()
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "database_url": database_url,
                "topics": ["process.motion.unique"],
                "cursor_topic": "analytics.persistence.cursor",
            }
        )
    )

    cursor_events = []
    done = asyncio.Event()

    async def handle_cursor(topic: str, payload) -> None:
        cursor_events.append(payload)
        done.set()

    bus.subscribe("analytics.persistence.cursor", handle_cursor)

    await module.start()
    detection = DetectionEvent(
        camera_id="lab",
        detector_id="yolo",
        timestamp_utc=dt.datetime.now(tz=dt.UTC),
    )
    await bus.publish("process.motion.unique", detection)
    await asyncio.wait_for(done.wait(), timeout=1.0)

    await module.stop()
    await bus.stop()

    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    with Session(engine) as session:
        result = session.exec(select(AnalyticsEvent)).first()
        assert result is not None
        assert result.topic == "process.motion.unique"
