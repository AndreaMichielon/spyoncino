import asyncio

import pytest

from spyoncino.core.contracts import BaseModule, HealthStatus, ModuleConfig
from spyoncino.core.orchestrator import Orchestrator


class _StubModule(BaseModule):
    name = "tests.stub.module"

    async def start(self) -> None:
        return None

    async def health(self) -> HealthStatus:
        return HealthStatus(status="healthy", details={})


@pytest.mark.asyncio
async def test_orchestrator_emits_health_summary() -> None:
    orchestrator = Orchestrator(health_interval=0.05, publish_health=True)
    summary_signal = asyncio.Event()
    summaries: list[str] = []

    async def handler(topic: str, payload) -> None:
        summaries.append(payload.status)
        summary_signal.set()

    orchestrator.bus.subscribe("status.health.summary", handler)
    await orchestrator.add_module(_StubModule(), ModuleConfig())
    await orchestrator.start()
    await asyncio.wait_for(summary_signal.wait(), timeout=0.5)
    await orchestrator.stop()

    assert summaries
    assert summaries[0] == "healthy"
