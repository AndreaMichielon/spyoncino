import asyncio

import pytest

from spyoncino.core.contracts import ConfigUpdate
from spyoncino.core.orchestrator import Orchestrator
from spyoncino.modules.output.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_config_hot_reload_updates_modules(sample_config_service) -> None:
    orchestrator = Orchestrator()
    rate_limiter = RateLimiter()
    await orchestrator.add_module(
        rate_limiter,
        config=sample_config_service.module_config_for("modules.output.rate_limiter"),
    )
    orchestrator.enable_config_hot_reload(sample_config_service)

    snapshot_event = asyncio.Event()

    async def snapshot_handler(topic: str, payload) -> None:
        snapshot_event.set()

    orchestrator.bus.subscribe("config.snapshot", snapshot_handler)
    await orchestrator.start()
    snapshot_event.clear()

    update = ConfigUpdate(source="test", changes={"rate_limit": {"max_events": 1}})
    await orchestrator.bus.publish("config.update", update)

    await asyncio.wait_for(snapshot_event.wait(), timeout=0.5)
    await orchestrator.stop()

    assert rate_limiter._max_events == 1
