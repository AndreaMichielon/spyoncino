import asyncio
from pathlib import Path

import pytest

from spyoncino.core.bus import EventBus
from spyoncino.core.contracts import ModuleConfig, SnapshotArtifact, StorageSyncResult
from spyoncino.modules.storage.s3_uploader import S3ArtifactUploader


class StubTransfer:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str]] = []

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self.uploads.append((filename, bucket, key))


class StubClient:
    def __init__(self) -> None:
        self.tag_calls: list[dict] = []

    def put_object_tagging(self, **kwargs) -> None:
        self.tag_calls.append(kwargs)

    def head_object(self, **_kwargs) -> dict[str, str]:
        return {"ETag": "etag", "VersionId": "1"}


@pytest.mark.asyncio
async def test_s3_uploader_emits_sync_events(tmp_path: Path) -> None:
    bus = EventBus(telemetry_enabled=False)
    await bus.start()

    artifact = tmp_path / "snapshots" / "demo.png"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"snapshot")

    transfer = StubTransfer()
    client = StubClient()

    module = S3ArtifactUploader(client_factory=lambda: (client, transfer))
    module.set_bus(bus)
    await module.configure(
        ModuleConfig(
            options={
                "enabled": True,
                "bucket": "test-bucket",
                "root_dir": str(tmp_path),
                "upload_topics": ["event.snapshot.ready"],
                "publish_topic": "storage.s3.synced",
            }
        )
    )

    sync_events: list[StorageSyncResult] = []
    done = asyncio.Event()

    async def handle_sync(topic: str, payload: StorageSyncResult) -> None:
        sync_events.append(payload)
        done.set()

    bus.subscribe("storage.s3.synced", handle_sync)

    await module.start()
    await bus.publish(
        "event.snapshot.ready",
        SnapshotArtifact(camera_id="lab", artifact_path=str(artifact)),
    )
    await asyncio.wait_for(done.wait(), timeout=1.0)

    await module.stop()
    await bus.stop()

    assert transfer.uploads
    assert sync_events[0].bucket == "test-bucket"
