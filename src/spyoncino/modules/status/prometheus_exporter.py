"""
Expose internal metrics via Prometheus.

The exporter subscribes to `status.bus` updates and renders them as gauges so
operators gain quick visibility without digging through logs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from prometheus_client import CollectorRegistry, Gauge, start_http_server

from ...core.bus import Subscription
from ...core.contracts import BaseModule, BusStatus, ModuleConfig

logger = logging.getLogger(__name__)


def _default_server_factory(
    port: int, addr: str, registry: CollectorRegistry
) -> object:  # pragma: no cover - thin wrapper
    return start_http_server(port=port, addr=addr, registry=registry)


class PrometheusExporter(BaseModule):
    """Status module that exports bus telemetry via HTTP."""

    name = "modules.status.prometheus_exporter"

    def __init__(
        self,
        *,
        registry: CollectorRegistry | None = None,
        server_factory: Callable[[int, str, CollectorRegistry], object] | None = None,
    ) -> None:
        super().__init__()
        self._registry = registry or CollectorRegistry()
        self._server_factory = server_factory or _default_server_factory
        self._server: object | None = None
        self._bus_topic = "status.bus"
        self._port = 9093
        self._addr = "127.0.0.1"
        self._subscription: Subscription | None = None
        self._queue_depth = Gauge(
            "spyoncino_bus_queue_depth",
            "Number of events currently waiting on the bus.",
            registry=self._registry,
        )
        self._queue_capacity = Gauge(
            "spyoncino_bus_queue_capacity",
            "Maximum queue capacity.",
            registry=self._registry,
        )
        self._lag_seconds = Gauge(
            "spyoncino_bus_lag_seconds",
            "Event dispatch lag in seconds.",
            registry=self._registry,
        )
        self._published_total = Gauge(
            "spyoncino_bus_published_total",
            "Total published events since startup.",
            registry=self._registry,
        )
        self._processed_total = Gauge(
            "spyoncino_bus_processed_total",
            "Total processed events since startup.",
            registry=self._registry,
        )
        self._dropped_total = Gauge(
            "spyoncino_bus_dropped_total",
            "Total dropped events.",
            registry=self._registry,
        )

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._port = int(options.get("port", self._port))
        self._addr = options.get("addr", self._addr)
        self._bus_topic = options.get("bus_topic", self._bus_topic)

    async def start(self) -> None:
        if self._server is None:
            self._server = self._server_factory(self._port, self._addr, self._registry)
            logger.info("Started Prometheus exporter on %s:%d", self._addr, self._port)
        self._subscription = self.bus.subscribe(self._bus_topic, self._handle_bus_status)

    async def stop(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription)
            self._subscription = None
        server = getattr(self._server, "shutdown", None)
        if callable(server):
            server()
        self._server = None

    async def _handle_bus_status(self, topic: str, payload: BusStatus) -> None:
        if not isinstance(payload, BusStatus):
            return
        self._queue_depth.set(payload.queue_depth)
        self._queue_capacity.set(payload.queue_capacity)
        self._lag_seconds.set(payload.lag_seconds)
        self._published_total.set(payload.published_total)
        self._processed_total.set(payload.processed_total)
        self._dropped_total.set(payload.dropped_total)


__all__ = ["PrometheusExporter"]
