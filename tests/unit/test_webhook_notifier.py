import asyncio
from pathlib import Path

import pytest

from spyoncino.core.contracts import MediaArtifact, ModuleConfig, SnapshotArtifact
from spyoncino.core.orchestrator import Orchestrator
from spyoncino.modules.output.webhook_notifier import WebhookNotifier


class StubWebhookClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.sent = asyncio.Event()

    async def send_json(
        self,
        *,
        url: str,
        payload: dict[str, object],
        method: str = "POST",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(
            {"url": url, "payload": payload, "method": method, "headers": headers or {}}
        )
        self.sent.set()


@pytest.mark.asyncio
async def test_webhook_notifier_posts_snapshot(tmp_path: Path) -> None:
    artifact_path = tmp_path / "snap.png"
    artifact_path.write_bytes(b"fake-bytes")

    client = StubWebhookClient()
    notifier = WebhookNotifier(client=client)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "url": "https://hooks.example.com/snapshot",
                "include_binary": True,
                "max_binary_bytes": 1024,
            }
        ),
    )

    await orchestrator.start()
    artifact = SnapshotArtifact(
        camera_id="garage",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion", "confidence": 0.8}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact)
    await asyncio.wait_for(client.sent.wait(), timeout=1.0)
    await orchestrator.stop()

    assert client.calls
    call = client.calls[0]
    assert call["url"] == "https://hooks.example.com/snapshot"
    payload = call["payload"]
    assert payload["camera_id"] == "garage"
    assert payload["artifact_base64"] is not None


@pytest.mark.asyncio
async def test_webhook_notifier_posts_clip(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"clip")

    client = StubWebhookClient()
    notifier = WebhookNotifier(client=client)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "clip_url": "https://hooks.example.com/clip",
            }
        ),
    )

    await orchestrator.start()
    clip = MediaArtifact(
        camera_id="porch",
        artifact_path=str(clip_path),
        media_kind="clip",
        metadata={"detection": {"detector_id": "motion"}},
    )
    await orchestrator.bus.publish("event.clip.ready", clip)
    await asyncio.wait_for(client.sent.wait(), timeout=1.0)
    await orchestrator.stop()

    assert client.calls
    call = client.calls[0]
    assert call["url"] == "https://hooks.example.com/clip"
    payload = call["payload"]
    assert payload["media_kind"] == "clip"
