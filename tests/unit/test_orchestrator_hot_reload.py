import asyncio

import pytest

from spyoncino.core.config import ConfigService
from spyoncino.core.contracts import BaseModule, ConfigUpdate, ModuleConfig
from spyoncino.core.orchestrator import Orchestrator


class ProbeRateLimiter(BaseModule):
    """Stub module that records applied configurations."""

    name = "modules.output.rate_limiter"

    def __init__(self) -> None:
        super().__init__()
        self.configs: list[ModuleConfig] = []
        self.reconfigured = asyncio.Event()

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        self.configs.append(config)
        self.reconfigured.set()

    async def start(self) -> None:
        return None


@pytest.mark.asyncio
async def test_config_update_triggers_module_reconfigure(
    sample_config_service: ConfigService,
) -> None:
    module = ProbeRateLimiter()
    orchestrator = Orchestrator()
    orchestrator.enable_config_hot_reload(sample_config_service)
    await orchestrator.add_module(module, sample_config_service.module_config_for(module.name))

    await orchestrator.start()
    assert module.configs[-1].options["max_events"] == 10

    module.reconfigured.clear()
    update = ConfigUpdate(source="test-suite", changes={"rate_limit": {"max_events": 2}})
    await orchestrator.bus.publish("config.update", update)
    await asyncio.wait_for(module.reconfigured.wait(), timeout=1)
    await orchestrator.stop()

    assert module.configs[-1].options["max_events"] == 2
    assert len(module.configs) >= 2
