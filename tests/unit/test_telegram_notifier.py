import asyncio
from pathlib import Path

import pytest

from spyoncino.core.contracts import MediaArtifact, ModuleConfig, SnapshotArtifact
from spyoncino.core.orchestrator import Orchestrator
from spyoncino.modules.output.telegram_notifier import TelegramNotifier


class StubSender:
    def __init__(self) -> None:
        self.photos: list[tuple[int | str, Path, str | None]] = []
        self.animations: list[tuple[int | str, Path, str | None]] = []
        self.videos: list[tuple[int | str, Path, str | None]] = []
        self.sent = asyncio.Event()

    async def send_photo(
        self,
        *,
        chat_id: int | str,
        file_path: Path,
        caption: str | None = None,
    ) -> None:
        self.photos.append((chat_id, file_path, caption))
        self.sent.set()

    async def send_animation(
        self,
        *,
        chat_id: int | str,
        file_path: Path,
        caption: str | None = None,
    ) -> None:
        self.animations.append((chat_id, file_path, caption))
        self.sent.set()

    async def send_video(
        self,
        *,
        chat_id: int | str,
        file_path: Path,
        caption: str | None = None,
    ) -> None:
        self.videos.append((chat_id, file_path, caption))
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

    assert sender.photos
    chat_id, recorded_path, caption = sender.photos[0]
    assert chat_id == 999
    assert recorded_path == artifact_path
    assert "lab" in (caption or "")


@pytest.mark.asyncio
async def test_telegram_notifier_handles_gif_and_clip(tmp_path: Path) -> None:
    gif_path = tmp_path / "motion.gif"
    gif_path.write_bytes(b"gif-bytes")
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"clip-bytes")

    sender = StubSender()
    notifier = TelegramNotifier(sender=sender)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 111,
                "gif_chat_id": 222,
                "clip_chat_id": 333,
                "gif_topic": "event.gif.ready",
                "clip_topic": "event.clip.ready",
            }
        ),
    )

    await orchestrator.start()
    gif = SnapshotArtifact(
        camera_id="front",
        artifact_path=str(gif_path),
        content_type="image/gif",
        metadata={"detection": {"detector_id": "yolo"}},
    )
    clip = MediaArtifact(
        camera_id="front",
        artifact_path=str(clip_path),
        media_kind="clip",
        content_type="video/mp4",
        metadata={"detection": {"detector_id": "yolo"}},
    )
    await orchestrator.bus.publish("event.gif.ready", gif)
    await orchestrator.bus.publish("event.clip.ready", clip)
    await asyncio.sleep(0.05)
    await orchestrator.stop()

    assert sender.animations and sender.animations[0][0] == 222
    assert sender.videos and sender.videos[0][0] == 333
