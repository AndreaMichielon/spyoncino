"""
Lifecycle coordinator for modular Spyoncino components.

The orchestrator owns the shared event bus, configures modules, and
coordinates their startup and shutdown order. This is a minimal skeleton
for Week 1 that prioritises clarity over advanced features like dynamic
reconfiguration.
"""

from __future__ import annotations

import logging

from .bus import EventBus
from .contracts import BaseModule, HealthStatus, ModuleConfig

logger = logging.getLogger(__name__)


class Orchestrator:
    """Manage module lifecycle and shared infrastructure."""

    def __init__(self, *, bus: EventBus | None = None) -> None:
        self.bus = bus or EventBus()
        self._modules: list[BaseModule] = []
        self._configs: dict[str, ModuleConfig] = {}
        self._running = False

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
        logger.info("Orchestrator started %d modules.", len(self._modules))

    async def stop(self) -> None:
        """Stop all modules and shut down the bus."""
        if not self._running:
            logger.warning("Orchestrator stop requested while not running.")
            return
        for module in reversed(self._modules):
            logger.info("Stopping module %s", module.name)
            await module.stop()
        await self.bus.stop()
        self._running = False
        logger.info("Orchestrator stopped.")

    async def health(self) -> dict[str, HealthStatus]:
        """Aggregate health information from all modules."""
        reports: dict[str, HealthStatus] = {}
        for module in self._modules:
            reports[module.name] = await module.health()
        return reports
