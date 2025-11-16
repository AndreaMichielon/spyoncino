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
import hashlib
import logging
from collections import OrderedDict
from collections.abc import Iterable

from .bus import EventBus, Subscription
from .config import ConfigService, ConfigSnapshot
from .contracts import (
    BaseModule,
    ConfigRollbackPayload,
    ConfigSnapshotPayload,
    ConfigUpdate,
    HealthStatus,
    HealthSummary,
    ModuleConfig,
    ShutdownProgress,
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
        self._configs: dict[BaseModule, ModuleConfig] = {}
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
        self._shutdown_topic = "status.shutdown.progress"
        self._rollback_topic = "config.rollback"
        self._drill_interval: float | None = None
        self._drill_task: asyncio.Task[None] | None = None
        self._drill_index = 0
        # Payload hygiene interceptor removed; producers must avoid inline bytes.
        # Track last computed start order for reverse-order shutdown when using dependencies.
        self._last_start_order: list[BaseModule] | None = None

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
        self._configs[module] = config
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
        start_order = self._compute_start_order()
        for module in start_order:
            logger.info("Starting module %s", module.name)
            await module.start()
        self._running = True
        self._last_start_order = start_order
        if self._config_service:
            await self._publish_config_snapshot(self._config_service.snapshot)
        if self._publish_health:
            self._health_task = asyncio.create_task(self._health_loop(), name="spyoncino-health")
        if self._drill_interval and self._config_service:
            self._drill_task = asyncio.create_task(
                self._rollback_drill_loop(), name="spyoncino-rollback-drill"
            )
        logger.info("Orchestrator started %d modules.", len(self._modules))

    async def stop(self) -> None:
        """Stop all modules and shut down the bus."""
        if not self._running:
            logger.warning("Orchestrator stop requested while not running.")
            return
        # Prefer reverse topological when dependencies were used; otherwise fallback to prefix phases
        if self._last_start_order:
            await self._stop_modules_reverse(self._last_start_order)
        else:
            await self._stop_modules_staged()
        if self._health_task:
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_task
            self._health_task = None
        if self._drill_task:
            self._drill_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drill_task
            self._drill_task = None
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

    # Note: bus interceptor for stripping inline bytes has been removed. Producers are expected
    # to persist frames to disk/object storage and publish only references (data_ref).

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
            prev_config = self._configs.get(module)
            camera_id: str | None = None
            if prev_config:
                camera_id = prev_config.options.get("camera_id")
            try:
                new_config = self._config_service.module_config_for(
                    module.name, camera_id=camera_id
                )
            except KeyError:
                logger.debug("No config builder registered for module %s", module.name)
                continue
            await module.configure(new_config)
            self._configs[module] = new_config
        await self._publish_config_snapshot(snapshot)

    async def _publish_config_snapshot(self, snapshot: ConfigSnapshot) -> None:
        payload = ConfigSnapshotPayload(data=snapshot.model_dump(mode="python"))
        await self.bus.publish(self._config_snapshot_topic, payload)

    def enable_rollback_drills(self, interval_hours: float = 168.0) -> None:
        """Schedule automatic rollback drills at the requested cadence."""
        self._drill_interval = interval_hours * 3600.0

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

    def _compute_start_order(self) -> list[BaseModule]:
        """
        Determine startup order for modules:
        - If any module declares depends_on, perform a topological sort with tie-breaking by registration order.
        - Otherwise, preserve registration order (legacy behavior).
        """
        dependencies = self._extract_dependencies()
        if not any(dependencies.values()):
            # No declared dependencies anywhere; keep legacy behavior
            return list(self._modules)
        # Map module instances to indices for tie-breaking and multiple instances
        index_of: dict[BaseModule, int] = {m: i for i, m in enumerate(self._modules)}
        # Resolve string dependency names to module instances, using the latest prior registration with matching name
        edges: dict[BaseModule, set[BaseModule]] = {m: set() for m in self._modules}
        all_known_names: set[str] = {m.name for m in self._modules}
        # Build quick name->list[BaseModule] mapping in registration order
        by_name: dict[str, list[BaseModule]] = {}
        for m in self._modules:
            by_name.setdefault(m.name, []).append(m)
        # Resolve edges
        for module, dep_names in dependencies.items():
            for dep_name in dep_names:
                if dep_name not in all_known_names:
                    raise RuntimeError(
                        f"Unknown dependency '{dep_name}' referenced by {module.name}"
                    )
                # Choose the most recent instance registered BEFORE this module
                candidates = by_name.get(dep_name, [])
                chosen: BaseModule | None = None
                for candidate in reversed(candidates):
                    if index_of[candidate] < index_of[module]:
                        chosen = candidate
                        break
                if chosen is None:
                    # No prior instance; fall back to first matching instance to avoid empty edge,
                    # but still provide a clear message since this likely indicates config ordering issues.
                    chosen = candidates[0]
                    logger.warning(
                        "Dependency '%s' for %s refers to a later or same-order instance; using first instance.",
                        dep_name,
                        module.name,
                    )
                edges[module].add(chosen)
        # Perform Kahn's algorithm
        incoming_count: dict[BaseModule, int] = {m: 0 for m in self._modules}
        for m, deps in edges.items():
            incoming_count[m] = len(deps)
        # Nodes with no incoming edges, keep registration order stability
        ready: list[BaseModule] = [m for m in self._modules if incoming_count[m] == 0]
        order: list[BaseModule] = []
        # Build reverse edges for efficient removal: provider -> set[dependents]
        reverse_edges: dict[BaseModule, set[BaseModule]] = {m: set() for m in self._modules}
        for m, deps in edges.items():
            for d in deps:
                reverse_edges[d].add(m)
        while ready:
            node = ready.pop(0)
            order.append(node)
            for dependent in reverse_edges[node]:
                incoming_count[dependent] -= 1
                if incoming_count[dependent] == 0:
                    # Preserve registration order by inserting based on original index
                    # but simple append keeps deterministic behavior since dependents only
                    # become ready once all their deps are in 'order'.
                    ready.append(dependent)
        if len(order) != len(self._modules):
            # Cycle detection: find nodes still with incoming edges
            cyclic = [m.name for m, cnt in incoming_count.items() if cnt > 0]
            raise RuntimeError(f"Dependency cycle detected among modules: {cyclic}")
        return order

    def _extract_dependencies(self) -> dict[BaseModule, list[str]]:
        """
        Read declared dependencies from each module's configuration options.
        Accepts a list of strings in ModuleConfig.options['depends_on'].
        """
        result: dict[BaseModule, list[str]] = {}
        for module in self._modules:
            config = self._configs.get(module)
            deps: list[str] = []
            if config is not None:
                value = config.options.get("depends_on")
                if isinstance(value, list | tuple):
                    deps = [str(x) for x in value if isinstance(x, str)]
                elif isinstance(value, str):
                    deps = [value]
                elif value is None:
                    deps = []
                else:
                    raise RuntimeError(
                        f"Invalid depends_on for {module.name}: expected list[str] or str, got {type(value).__name__}"
                    )
            result[module] = deps
        return result

    async def _stop_modules_staged(self) -> None:
        phased_modules = self._group_modules_by_phase()
        total_phases = len(phased_modules)
        for index, (phase, modules) in enumerate(phased_modules.items()):
            for module in modules:
                await self._publish_shutdown_progress(
                    phase, index, total_phases, module.name, "starting"
                )
                try:
                    await module.stop()
                except Exception as exc:  # pragma: no cover - logged for troubleshooting
                    logger.exception("Module %s failed to stop cleanly.", module.name)
                    await self._publish_shutdown_progress(
                        phase, index, total_phases, module.name, "failed", str(exc)
                    )
                else:
                    await self._publish_shutdown_progress(
                        phase, index, total_phases, module.name, "completed"
                    )

    async def _publish_shutdown_progress(
        self,
        phase: str,
        phase_index: int,
        total_phases: int,
        module_name: str,
        status: str,
        message: str | None = None,
    ) -> None:
        payload = ShutdownProgress(
            phase=phase,
            phase_index=phase_index,
            total_phases=total_phases,
            module=module_name,
            status=status,  # type: ignore[arg-type]
            message=message,
        )
        await self.bus.publish(self._shutdown_topic, payload)

    def _group_modules_by_phase(self) -> OrderedDict[str, list[BaseModule]]:
        phase_order = OrderedDict(
            [
                ("outputs", []),
                ("dashboard", []),
                ("events", []),
                ("process", []),
                ("inputs", []),
                ("storage", []),
                ("analytics", []),
                ("status", []),
                ("other", []),
            ]
        )
        for module in self._modules[::-1]:  # preserve startup order within phase
            prefix = module.name
            if prefix.startswith("modules.output."):
                phase_order["outputs"].append(module)
            elif prefix.startswith("modules.dashboard."):
                phase_order["dashboard"].append(module)
            elif prefix.startswith("modules.event."):
                phase_order["events"].append(module)
            elif prefix.startswith("modules.process."):
                phase_order["process"].append(module)
            elif prefix.startswith("modules.input."):
                phase_order["inputs"].append(module)
            elif prefix.startswith("modules.storage."):
                phase_order["storage"].append(module)
            elif prefix.startswith("modules.analytics."):
                phase_order["analytics"].append(module)
            elif prefix.startswith("modules.status."):
                phase_order["status"].append(module)
            else:
                phase_order["other"].append(module)
        return OrderedDict((phase, modules) for phase, modules in phase_order.items() if modules)

    async def _stop_modules_reverse(self, start_order: Iterable[BaseModule]) -> None:
        """
        Stop modules in reverse of the provided start order, emitting shutdown progress.
        """
        ordered = list(start_order)[::-1]
        total = len(ordered)
        # Use a synthetic 'dependency' phase label for clarity
        phase = "dependency"
        for index, module in enumerate(ordered):
            await self._publish_shutdown_progress(phase, index, total, module.name, "starting")
            try:
                await module.stop()
            except Exception as exc:  # pragma: no cover
                logger.exception("Module %s failed to stop cleanly.", module.name)
                await self._publish_shutdown_progress(
                    phase, index, total, module.name, "failed", str(exc)
                )
            else:
                await self._publish_shutdown_progress(phase, index, total, module.name, "completed")

    async def _rollback_drill_loop(self) -> None:
        try:
            while self._running and self._drill_interval:
                await asyncio.sleep(self._drill_interval)
                await self._run_single_drill()
        except asyncio.CancelledError:  # pragma: no cover
            return

    async def _run_single_drill(self) -> None:
        if not self._modules or not self._config_service:
            return
        module = self._modules[self._drill_index % len(self._modules)]
        self._drill_index += 1
        logger.info("Rollback drill restarting module %s", module.name)
        success = True
        message: str | None = None
        try:
            await module.stop()
            config = self._configs.get(module)
            if config is None:
                logger.warning("No stored config for module %s; skipping drill", module.name)
                return
            await module.configure(config)
            await module.start()
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Rollback drill failed for module %s", module.name)
            success = False
            message = str(exc)
        snapshot = self._config_service.snapshot
        fingerprint = self._snapshot_fingerprint(snapshot)
        payload = ConfigRollbackPayload(
            reason="scheduled_drill",
            module=module.name,
            success=success,
            snapshot_fingerprint=fingerprint,
            details={"message": message} if message else {},
        )
        await self.bus.publish(self._rollback_topic, payload)

    @staticmethod
    def _snapshot_fingerprint(snapshot: ConfigSnapshot) -> str:
        raw = snapshot.model_dump_json(sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
