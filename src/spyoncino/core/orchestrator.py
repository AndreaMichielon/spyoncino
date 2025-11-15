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

from .bus import EventBus, Subscription
from .config import ConfigService, ConfigSnapshot
from .contracts import (
    BaseModule,
    ConfigSnapshotPayload,
    ConfigUpdate,
    HealthStatus,
    HealthSummary,
    ModuleConfig,
)

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
        self._config_service: ConfigService | None = None
        self._config_update_topic = "config.update"
        self._config_snapshot_topic = "config.snapshot"
        self._config_subscription: Subscription | None = None
        self._config_lock = asyncio.Lock()

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

    def enable_config_hot_reload(
        self,
        config_service: ConfigService,
        *,
        update_topic: str = "config.update",
        snapshot_topic: str = "config.snapshot",
    ) -> None:
        """
        Enable config hot reload by subscribing to update events on the bus.
        """
        if self._config_service is not None:
            raise RuntimeError("Config hot reload already enabled.")
        self._config_service = config_service
        self._config_update_topic = update_topic
        self._config_snapshot_topic = snapshot_topic
        self._config_subscription = self.bus.subscribe(update_topic, self._handle_config_update)
        logger.info(
            "Config hot reload enabled; listening for updates on %s", self._config_update_topic
        )

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
        if self._config_service:
            await self._publish_config_snapshot(self._config_service.snapshot)
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
        if self._config_subscription:
            self.bus.unsubscribe(self._config_subscription)
            self._config_subscription = None
        await self.bus.stop()
        self._running = False
        logger.info("Orchestrator stopped.")

    async def health(self) -> dict[str, HealthStatus]:
        """Aggregate health information from all modules."""
        reports: dict[str, HealthStatus] = {}
        for module in self._modules:
            reports[module.name] = await module.health()
        return reports

    async def _handle_config_update(self, topic: str, payload: ConfigUpdate) -> None:
        if not isinstance(payload, ConfigUpdate):
            logger.debug("Ignoring non ConfigUpdate payload on %s", topic)
            return
        if self._config_service is None:
            logger.warning("Received config update without config service attached.")
            return
        async with self._config_lock:
            if payload.reload or not payload.changes:
                snapshot = self._config_service.refresh()
            else:
                snapshot = self._config_service.apply_changes(payload.changes)
            await self._apply_config_snapshot(snapshot)

    async def _apply_config_snapshot(self, snapshot: ConfigSnapshot) -> None:
        if self._config_service is None:
            return
        logger.info("Applying refreshed configuration to %d modules", len(self._modules))
        for module in self._modules:
            try:
                new_config = self._config_service.module_config_for(module.name)
            except KeyError:
                logger.debug("No config builder registered for module %s", module.name)
                continue
            await module.configure(new_config)
            self._configs[module.name] = new_config
        await self._publish_config_snapshot(snapshot)

    async def _publish_config_snapshot(self, snapshot: ConfigSnapshot) -> None:
        payload = ConfigSnapshotPayload(data=snapshot.model_dump(mode="python"))
        await self.bus.publish(self._config_snapshot_topic, payload)

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
