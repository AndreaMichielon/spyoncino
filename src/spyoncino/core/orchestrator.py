"""
Lifecycle coordinator for modular Spyoncino components.

The orchestrator owns the shared event bus, configures modules, and
coordinates their startup and shutdown order. This is a minimal skeleton
for Week 1 that prioritises clarity over advanced features like dynamic
reconfiguration.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .bus import EventBus
from .contracts import BaseModule, HealthStatus, HealthSummary, ModuleConfig

logger = logging.getLogger(__name__)


class Orchestrator:
    """Manage module lifecycle and shared infrastructure."""

    def __init__(
        self,
        *,
        bus: EventBus | None = None,
        health_interval: float = 10.0,
        publish_health: bool = True,
        health_topic: str = "status.health.summary",
    ) -> None:
        self.bus = bus or EventBus()
        self._modules: list[BaseModule] = []
        self._configs: dict[str, ModuleConfig] = {}
        self._running = False
        self._health_interval = health_interval
        self._publish_health = publish_health
        self._health_topic = health_topic
        self._health_task: asyncio.Task[None] | None = None

    async def add_module(self, module: BaseModule, config: ModuleConfig | None = None) -> None:
        """
        Register a module with an optional configuration.

        Configuration defaults to the module's baseline if one is not
        provided. Modules receive the shared bus before configuration.
        """
        module.set_bus(self.bus)
        if config is None:
            config = ModuleConfig()
        await module.configure(config)
        self._modules.append(module)
        self._configs[module.name] = config
        logger.info("Registered module %s", module.name)

    async def start(self) -> None:
        """Start the bus and all registered modules."""
        if self._running:
            logger.warning("Orchestrator already running.")
            return
        await self.bus.start()
        for module in self._modules:
            logger.info("Starting module %s", module.name)
            await module.start()
        self._running = True
        if self._publish_health:
            self._health_task = asyncio.create_task(self._health_loop(), name="spyoncino-health")
        logger.info("Orchestrator started %d modules.", len(self._modules))

    async def stop(self) -> None:
        """Stop all modules and shut down the bus."""
        if not self._running:
            logger.warning("Orchestrator stop requested while not running.")
            return
        for module in reversed(self._modules):
            logger.info("Stopping module %s", module.name)
            await module.stop()
        if self._health_task:
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_task
            self._health_task = None
        await self.bus.stop()
        self._running = False
        logger.info("Orchestrator stopped.")

    async def health(self) -> dict[str, HealthStatus]:
        """Aggregate health information from all modules."""
        reports: dict[str, HealthStatus] = {}
        for module in self._modules:
            reports[module.name] = await module.health()
        return reports

    async def _health_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._health_interval)
                reports = await self.health()
                overall = self._determine_overall_status(reports)
                payload = HealthSummary(status=overall, modules=reports)
                await self.bus.publish(self._health_topic, payload)
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            return

    @staticmethod
    def _determine_overall_status(reports: dict[str, HealthStatus]) -> str:
        statuses = {report.status for report in reports.values()}
        if "error" in statuses:
            return "error"
        if "degraded" in statuses:
            return "degraded"
        return "healthy"
