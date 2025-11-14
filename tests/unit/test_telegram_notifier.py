import asyncio
from pathlib import Path

import pytest

from spyoncino.core.contracts import ModuleConfig, SnapshotArtifact
from spyoncino.core.orchestrator import Orchestrator
from spyoncino.modules.output.telegram_notifier import TelegramNotifier


class StubSender:
    def __init__(self) -> None:
        self.messages: list[tuple[int | str, Path, str | None]] = []
        self.sent = asyncio.Event()

    async def send_photo(
        self,
        *,
        chat_id: int | str,
        file_path: Path,
        caption: str | None = None,
    ) -> None:
        self.messages.append((chat_id, file_path, caption))
        self.sent.set()


@pytest.mark.asyncio
async def test_telegram_notifier_uses_sender(tmp_path: Path) -> None:
    artifact_path = tmp_path / "snap.png"
    artifact_path.write_bytes(b"fake-bytes")

    sender = StubSender()
    notifier = TelegramNotifier(sender=sender)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 999,
                "topic": "event.snapshot.ready",
            }
        ),
    )

    await orchestrator.start()
    artifact = SnapshotArtifact(
        camera_id="lab",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion-basic", "confidence": 0.9}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)
    await orchestrator.stop()

    assert sender.messages
    chat_id, recorded_path, caption = sender.messages[0]
    assert chat_id == 999
    assert recorded_path == artifact_path
    assert "lab" in (caption or "")
