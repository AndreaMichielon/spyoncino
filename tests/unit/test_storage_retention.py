import asyncio
import datetime as dt
import os
from pathlib import Path

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import ModuleConfig, StorageStats
from spyoncino.modules.storage.retention import StorageRetention


@pytest.mark.asyncio
async def test_storage_retention_emits_stats_and_prunes(tmp_path: Path) -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    snapshots = tmp_path / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    old_file = snapshots / "old.png"
    old_file.write_bytes(b"old")

    # Ensure file is in the past so retention deletes it.
    past = dt.datetime(2020, 1, 1, tzinfo=dt.UTC).timestamp()
    Path(old_file).touch()
    os.utime(old_file, (past, past))

    stats_received: list[StorageStats] = []
    stats_event = asyncio.Event()

    async def handle_stats(topic: str, payload: StorageStats) -> None:
        stats_received.append(payload)
        stats_event.set()

    bus.subscribe("storage.stats", handle_stats)

    module = StorageRetention()
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "root_dir": str(tmp_path),
                "retention_hours": 0,
                "cleanup_interval_seconds": 0.05,
                "artifact_globs": ["snapshots/*.png"],
            }
        )
    )

    await module.start()
    await asyncio.wait_for(stats_event.wait(), timeout=1.0)
    await module.stop()
    await bus.stop()

    assert stats_received
    assert stats_received[0].files_deleted >= 1
    assert not old_file.exists()
