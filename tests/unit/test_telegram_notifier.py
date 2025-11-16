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
        self.messages: list[tuple[int | str, str]] = []
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

    async def send_message(
        self,
        *,
        chat_id: int | str,
        text: str,
    ) -> None:
        self.messages.append((chat_id, text))
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
async def test_telegram_notifier_sends_text_when_configured(tmp_path: Path) -> None:
    artifact_path = tmp_path / "snap.png"
    artifact_path.write_text("unused")

    sender = StubSender()
    notifier = TelegramNotifier(sender=sender)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 321,
                "topic": "event.snapshot.ready",
                "snapshot_delivery": "text",
            }
        ),
    )

    await orchestrator.start()
    artifact = SnapshotArtifact(
        camera_id="lab",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion-basic", "confidence": 0.4}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)
    await orchestrator.stop()

    assert len(sender.messages) == 1
    chat_id, text = sender.messages[0]
    assert chat_id == 321
    assert "lab" in text
    assert not sender.photos


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


@pytest.mark.asyncio
async def test_telegram_notifier_broadcasts_to_all_targets(tmp_path: Path) -> None:
    artifact_path = tmp_path / "snap.mp4"
    artifact_path.write_bytes(b"bytes")
    sender = StubSender()
    notifier = TelegramNotifier(sender=sender)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_targets": [111, 222],
                "gif_chat_targets": [333, 444],
                "gif_topic": "event.gif.ready",
            }
        ),
    )

    await orchestrator.start()
    artifact = SnapshotArtifact(
        camera_id="yard",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion-basic"}},
    )
    gif = SnapshotArtifact(
        camera_id="yard",
        artifact_path=str(artifact_path),
        content_type="image/gif",
        metadata={"detection": {"detector_id": "motion-basic"}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact)
    await orchestrator.bus.publish("event.gif.ready", gif)
    await asyncio.sleep(0.05)
    await orchestrator.stop()

    assert sorted(chat for chat, *_ in sender.photos) == [111, 222]
    gif_chats = {
        *[chat for chat, *_ in sender.animations],
        *[chat for chat, *_ in sender.videos],
    }
    assert sorted(gif_chats) == [333, 444]


@pytest.mark.asyncio
async def test_telegram_notifier_transcodes_large_gifs(tmp_path: Path) -> None:
    gif_path = tmp_path / "event.gif"
    gif_path.write_bytes(b"x" * 2048)
    converted_path = tmp_path / "converted.mp4"

    sender = StubSender()
    notifier = TelegramNotifier(sender=sender)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 777,
                "gif_topic": "event.gif.ready",
                "transcode_large_gifs": True,
                "inline_animation_max_mb": 0.0001,
            }
        ),
    )

    def fake_transcode(self, _gif_path: Path) -> Path:
        converted_path.write_bytes(b"mp4-bytes")
        return converted_path

    notifier._transcode_gif_to_mp4 = fake_transcode.__get__(notifier, TelegramNotifier)

    await orchestrator.start()
    gif = SnapshotArtifact(
        camera_id="yard",
        artifact_path=str(gif_path),
        content_type="image/gif",
        metadata={},
    )
    await orchestrator.bus.publish("event.gif.ready", gif)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)
    await orchestrator.stop()

    assert sender.videos, "Expected GIF to be sent as video after transcode"
    assert sender.videos[0][1] == converted_path
    assert not converted_path.exists(), "Temporary MP4 should be cleaned up"
