"""
Chaos/resilience helper that injects latency and drop scenarios via bus interceptors.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import asdict, dataclass
from typing import Any

from ...core.contracts import (
    BaseModule,
    BasePayload,
    ControlCommand,
    HealthStatus,
    ModuleConfig,
    ResilienceEvent,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Scenario:
    name: str
    topic: str
    latency_seconds: float
    drop_probability: float
    enabled: bool = True


class ResilienceTester(BaseModule):
    """Inject latency/drops for configured topics to validate failover paths."""

    name = "modules.status.resilience_tester"

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._scenarios: dict[str, Scenario] = {}
        self._command_topic = "dashboard.control.command"
        self._status_topic = "status.resilience.event"
        self._subscription = None
        self._rand = random.Random()  # nosec B311 - pseudo RNG sufficient for chaos testing

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._enabled = bool(options.get("enabled", self._enabled))
        self._command_topic = options.get("command_topic", self._command_topic)
        self._status_topic = options.get("status_topic", self._status_topic)
        self._scenarios = {}
        for raw in options.get("scenarios", []):
            try:
                scenario = Scenario(
                    name=str(raw.get("name")),
                    topic=str(raw.get("topic")),
                    latency_seconds=float(raw.get("latency_ms", 0.0)) / 1000.0,
                    drop_probability=float(raw.get("drop_probability", 0.0)),
                    enabled=bool(raw.get("enabled", True)),
                )
            except Exception:  # pragma: no cover - defensive validation
                logger.exception("Invalid resilience scenario %s", raw)
                continue
            self._scenarios[scenario.name] = scenario

    async def start(self) -> None:
        if not self._enabled:
            logger.info("ResilienceTester disabled.")
            return
        self.bus.add_interceptor(self._inject_faults)
        self._subscription = self.bus.subscribe(self._command_topic, self._handle_command)
        await self._publish_event("status", {"message": "resilience tester online"})

    async def stop(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription)
            self._subscription = None
        if self._enabled:
            self.bus.remove_interceptor(self._inject_faults)
        await self._publish_event("status", {"message": "resilience tester offline"})

    async def health(self) -> HealthStatus:
        enabled = self._enabled
        details = {
            "scenarios": {name: asdict(scenario) for name, scenario in self._scenarios.items()},
            "command_topic": self._command_topic,
        }
        status = "healthy" if enabled else "disabled"
        return HealthStatus(status=status, details=details)

    async def _inject_faults(
        self, topic: str, payload: BasePayload
    ) -> tuple[str, BasePayload] | None:
        if not self._enabled:
            return (topic, payload)
        for scenario in self._scenarios.values():
            if not scenario.enabled or scenario.topic != topic:
                continue
            if scenario.latency_seconds > 0:
                await asyncio.sleep(scenario.latency_seconds)
                await self._publish_event(
                    "latency",
                    {"scenario": scenario.name, "topic": topic, "delay": scenario.latency_seconds},
                )
            if scenario.drop_probability > 0 and self._rand.random() < scenario.drop_probability:
                await self._publish_event(
                    "drop",
                    {
                        "scenario": scenario.name,
                        "topic": topic,
                        "probability": scenario.drop_probability,
                    },
                )
                return None
        return (topic, payload)

    async def _handle_command(self, topic: str, payload: ControlCommand) -> None:
        if not isinstance(payload, ControlCommand):
            return
        if payload.command != "resilience.toggle":
            return
        name = payload.arguments.get("name")
        enabled = payload.arguments.get("enabled")
        if name not in self._scenarios or enabled is None:
            return
        scenario = self._scenarios[name]
        scenario.enabled = bool(enabled)
        await self._publish_event(
            "enable" if scenario.enabled else "disable",
            {"scenario": scenario.name},
        )

    async def _publish_event(self, action: str, details: dict[str, Any]) -> None:
        event = ResilienceEvent(
            scenario=details.get("scenario", action),
            topic=details.get("topic"),
            action=action,
            details=details,
        )
        await self.bus.publish(self._status_topic, event)


__all__ = ["ResilienceTester", "Scenario"]
