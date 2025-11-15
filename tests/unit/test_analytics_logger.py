import asyncio

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import DetectionEvent, ModuleConfig, StorageStats
from spyoncino.modules.analytics.event_logger import AnalyticsEventLogger


class StubEventLogger:
    def __init__(self) -> None:
        self.events = []

    def log_event(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_analytics_logger_records_events() -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    stub_logger = StubEventLogger()

    module = AnalyticsEventLogger(logger_factory=lambda **_: stub_logger)
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "detection_topics": ["process.motion.unique"],
                "alert_topics": ["process.alert.detected"],
                "storage_topic": "storage.stats",
                "db_path": "events.db",
            }
        )
    )

    await module.start()
    detection = DetectionEvent(camera_id="cam", detector_id="motion")
    await bus.publish("process.motion.unique", detection)
    await bus.publish(
        "storage.stats",
        StorageStats(
            root="/tmp",
            total_gb=10,
            used_gb=9,
            free_gb=1,
            usage_percent=90,
            warning=True,
        ),
    )
    await asyncio.sleep(0.05)
    await module.stop()
    await bus.stop()

    assert len(stub_logger.events) >= 2
