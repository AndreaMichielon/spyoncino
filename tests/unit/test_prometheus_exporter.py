import asyncio

import pytest
from prometheus_client import CollectorRegistry

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import BusStatus, ModuleConfig
from spyoncino.modules.status.prometheus_exporter import PrometheusExporter


class FakeServer:
    def __init__(self) -> None:
        self.shutdown_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.mark.asyncio
async def test_prometheus_exporter_tracks_bus_status() -> None:
    registry = CollectorRegistry()
    started = {}

    def factory(port: int, addr: str, _registry: CollectorRegistry) -> FakeServer:
        started["port"] = port
        started["addr"] = addr
        started["registry"] = _registry
        return FakeServer()

    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    module = PrometheusExporter(registry=registry, server_factory=factory)
    module.set_bus(bus)
    await module.configure(ModuleConfig(options={"port": 9999, "addr": "127.0.0.1"}))
    await module.start()

    await bus.publish(
        "status.bus",
        BusStatus(
            queue_depth=1,
            queue_capacity=8,
            subscriber_count=1,
            topic_count=1,
            published_total=5,
            processed_total=4,
            dropped_total=0,
            lag_seconds=0.1,
            watermark="normal",
        ),
    )
    await asyncio.sleep(0.05)

    await module.stop()
    await bus.stop()

    assert started["port"] == 9999
    assert registry.get_sample_value("spyoncino_bus_queue_depth") == 1
    assert registry.get_sample_value("spyoncino_bus_processed_total") == 4
