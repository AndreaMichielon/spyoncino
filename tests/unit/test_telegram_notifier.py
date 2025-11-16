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


# =============================================================================
# Cooldown/Filtering Tests
# =============================================================================


class MockClock:
    """Mock clock for controlling time in tests."""

    def __init__(self, start_time: float = 0.0) -> None:
        self._time = start_time

    def __call__(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


@pytest.mark.asyncio
async def test_cooldown_suppresses_duplicate_notifications(tmp_path: Path) -> None:
    """Test that notifications within cooldown period are suppressed."""
    artifact_path = tmp_path / "snap.png"
    artifact_path.write_bytes(b"fake-bytes")

    clock = MockClock(start_time=100.0)
    sender = StubSender()
    notifier = TelegramNotifier(sender=sender, clock=clock)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 999,
                "topic": "event.snapshot.ready",
                "cooldown_enabled": True,
                "cooldown_seconds": 30.0,
            }
        ),
    )

    await orchestrator.start()

    # First notification should pass
    artifact1 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion", "confidence": 0.9}},
    )
    sender.sent.clear()
    await orchestrator.bus.publish("event.snapshot.ready", artifact1)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)

    # Second notification within cooldown should be suppressed
    clock.advance(10.0)  # Only 10 seconds passed, less than 30s cooldown
    sender.sent.clear()
    artifact2 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion", "confidence": 0.9}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact2)
    await asyncio.sleep(0.1)  # Give time for handler to run and suppress

    # Third notification after cooldown should pass
    clock.advance(25.0)  # Now 35 seconds total, past cooldown
    sender.sent.clear()
    artifact3 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion", "confidence": 0.9}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact3)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)

    await orchestrator.stop()

    # Should only have 2 notifications (first and third)
    assert len(sender.photos) == 2, f"Expected 2 photos, got {len(sender.photos)}"


@pytest.mark.asyncio
async def test_cooldown_tracks_per_camera(tmp_path: Path) -> None:
    """Test that cooldown is tracked separately per camera."""
    artifact_path = tmp_path / "snap.png"
    artifact_path.write_bytes(b"fake-bytes")

    clock = MockClock(start_time=100.0)
    sender = StubSender()
    notifier = TelegramNotifier(sender=sender, clock=clock)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 999,
                "topic": "event.snapshot.ready",
                "cooldown_enabled": True,
                "cooldown_seconds": 30.0,
            }
        ),
    )

    await orchestrator.start()

    # Notification from camera1
    artifact1 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion"}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact1)
    await asyncio.sleep(0.05)

    # Notification from camera2 immediately after should pass (different camera)
    clock.advance(1.0)
    artifact2 = SnapshotArtifact(
        camera_id="camera2",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion"}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact2)
    await asyncio.sleep(0.05)

    await orchestrator.stop()

    # Both should pass because they're from different cameras
    assert len(sender.photos) == 2, f"Expected 2 photos (one per camera), got {len(sender.photos)}"


@pytest.mark.asyncio
async def test_cooldown_tracks_per_notification_type(tmp_path: Path) -> None:
    """Test that cooldown is tracked separately per notification type (snapshot/gif/clip)."""
    snapshot_path = tmp_path / "snap.png"
    snapshot_path.write_bytes(b"fake-bytes")
    gif_path = tmp_path / "motion.gif"
    gif_path.write_bytes(b"gif-bytes")

    clock = MockClock(start_time=100.0)
    sender = StubSender()
    notifier = TelegramNotifier(sender=sender, clock=clock)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 999,
                "topic": "event.snapshot.ready",
                "gif_topic": "event.gif.ready",
                "cooldown_enabled": True,
                "cooldown_seconds": 30.0,
            }
        ),
    )

    await orchestrator.start()

    # Snapshot notification
    snapshot = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(snapshot_path),
        metadata={"detection": {"detector_id": "motion"}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", snapshot)
    await asyncio.sleep(0.05)

    # GIF notification immediately after should pass (different type)
    clock.advance(1.0)
    gif = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(gif_path),
        content_type="image/gif",
        metadata={"detection": {"detector_id": "yolo"}},
    )
    await orchestrator.bus.publish("event.gif.ready", gif)
    await asyncio.sleep(0.05)

    await orchestrator.stop()

    # Both should pass because they're different notification types
    assert len(sender.photos) == 1, "Expected 1 snapshot"
    assert len(sender.animations) == 1, "Expected 1 GIF"


@pytest.mark.asyncio
async def test_bbox_overlap_suppresses_duplicate_detections(tmp_path: Path) -> None:
    """Test that notifications with overlapping bboxes are suppressed."""
    artifact_path = tmp_path / "snap.png"
    artifact_path.write_bytes(b"fake-bytes")

    clock = MockClock(start_time=100.0)
    sender = StubSender()
    notifier = TelegramNotifier(sender=sender, clock=clock)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 999,
                "topic": "event.snapshot.ready",
                "cooldown_enabled": True,
                "cooldown_seconds": 30.0,
                "bbox_iou_threshold": 0.6,
                "timeout_seconds": 5.0,
            }
        ),
    )

    await orchestrator.start()

    # First notification with bbox
    artifact1 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={
            "detection": {
                "detector_id": "yolo",
                "attributes": {"bbox": [10.0, 10.0, 50.0, 50.0]},
            }
        },
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact1)
    await asyncio.sleep(0.05)

    # Second notification with overlapping bbox (after cooldown but within timeout)
    clock.advance(35.0)  # Past cooldown
    artifact2 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={
            "detection": {
                "detector_id": "yolo",
                "attributes": {"bbox": [15.0, 15.0, 55.0, 55.0]},  # Overlapping
            }
        },
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact2)
    await asyncio.sleep(0.05)

    # Third notification with non-overlapping bbox should pass
    clock.advance(1.0)
    artifact3 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={
            "detection": {
                "detector_id": "yolo",
                "attributes": {"bbox": [200.0, 200.0, 250.0, 250.0]},  # Far away
            }
        },
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact3)
    await asyncio.sleep(0.05)

    await orchestrator.stop()

    # Should have 2 notifications (first and third, second suppressed due to overlap)
    assert len(sender.photos) == 2, f"Expected 2 photos, got {len(sender.photos)}"


@pytest.mark.asyncio
async def test_timeout_resets_cooldown(tmp_path: Path) -> None:
    """Test that cooldown resets after timeout period."""
    artifact_path = tmp_path / "snap.png"
    artifact_path.write_bytes(b"fake-bytes")

    clock = MockClock(start_time=100.0)
    sender = StubSender()
    notifier = TelegramNotifier(sender=sender, clock=clock)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 999,
                "topic": "event.snapshot.ready",
                "cooldown_enabled": True,
                # Use a short cooldown so the second notification clearly occurs after it
                "cooldown_seconds": 5.0,
            }
        ),
    )

    await orchestrator.start()

    # First notification
    artifact1 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion"}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact1)
    await asyncio.sleep(0.05)

    # Second notification after cooldown should pass
    clock.advance(6.0)  # Past cooldown (5s), so it should be allowed
    artifact2 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion"}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact2)
    await asyncio.sleep(0.05)

    await orchestrator.stop()

    # Both should pass because timeout reset the cooldown
    assert len(sender.photos) == 2, f"Expected 2 photos after timeout, got {len(sender.photos)}"


@pytest.mark.asyncio
async def test_cooldown_disabled_allows_all_notifications(tmp_path: Path) -> None:
    """Test that when cooldown is disabled, all notifications pass through."""
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
                "cooldown_enabled": False,  # Disabled
                "cooldown_seconds": 30.0,
            }
        ),
    )

    await orchestrator.start()

    # Send multiple notifications rapidly
    for _ in range(5):
        artifact = SnapshotArtifact(
            camera_id="camera1",
            artifact_path=str(artifact_path),
            metadata={"detection": {"detector_id": "motion"}},
        )
        await orchestrator.bus.publish("event.snapshot.ready", artifact)
        await asyncio.sleep(0.01)

    await asyncio.sleep(0.1)
    await orchestrator.stop()

    # All should pass when cooldown is disabled
    assert (
        len(sender.photos) == 5
    ), f"Expected 5 photos when cooldown disabled, got {len(sender.photos)}"


@pytest.mark.asyncio
async def test_cooldown_without_bbox_works(tmp_path: Path) -> None:
    """Test that cooldown works even when artifacts don't have bbox information."""
    artifact_path = tmp_path / "snap.png"
    artifact_path.write_bytes(b"fake-bytes")

    clock = MockClock(start_time=100.0)
    sender = StubSender()
    notifier = TelegramNotifier(sender=sender, clock=clock)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 999,
                "topic": "event.snapshot.ready",
                "cooldown_enabled": True,
                "cooldown_seconds": 30.0,
            }
        ),
    )

    await orchestrator.start()

    # First notification without bbox
    artifact1 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion"}},  # No bbox
    )
    sender.sent.clear()
    await orchestrator.bus.publish("event.snapshot.ready", artifact1)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)

    # Second notification without bbox within cooldown should be suppressed
    clock.advance(10.0)
    sender.sent.clear()
    artifact2 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion"}},  # No bbox
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact2)
    await asyncio.sleep(0.1)  # Give time for handler to run and suppress

    await orchestrator.stop()

    # Only first should pass
    assert len(sender.photos) == 1, f"Expected 1 photo, got {len(sender.photos)}"


@pytest.mark.asyncio
async def test_cooldown_applies_to_all_notification_types(tmp_path: Path) -> None:
    """Test that cooldown applies to snapshots, GIFs, and clips."""
    snapshot_path = tmp_path / "snap.png"
    snapshot_path.write_bytes(b"fake-bytes")
    gif_path = tmp_path / "motion.gif"
    gif_path.write_bytes(b"gif-bytes")
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"clip-bytes")

    clock = MockClock(start_time=100.0)
    sender = StubSender()
    notifier = TelegramNotifier(sender=sender, clock=clock)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "chat_id": 999,
                "topic": "event.snapshot.ready",
                "gif_topic": "event.gif.ready",
                "clip_topic": "event.clip.ready",
                "cooldown_enabled": True,
                "cooldown_seconds": 30.0,
            }
        ),
    )

    await orchestrator.start()

    # First snapshot
    snapshot1 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(snapshot_path),
        metadata={"detection": {"detector_id": "motion"}},
    )
    sender.sent.clear()
    await orchestrator.bus.publish("event.snapshot.ready", snapshot1)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)

    # Second snapshot within cooldown - should be suppressed
    clock.advance(10.0)
    sender.sent.clear()
    snapshot2 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(snapshot_path),
        metadata={"detection": {"detector_id": "motion"}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", snapshot2)
    await asyncio.sleep(0.1)  # Give time for handler to run and suppress

    # First GIF
    gif1 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(gif_path),
        content_type="image/gif",
        metadata={"detection": {"detector_id": "yolo"}},
    )
    sender.sent.clear()
    await orchestrator.bus.publish("event.gif.ready", gif1)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)

    # Second GIF within cooldown - should be suppressed
    clock.advance(10.0)
    sender.sent.clear()
    gif2 = SnapshotArtifact(
        camera_id="camera1",
        artifact_path=str(gif_path),
        content_type="image/gif",
        metadata={"detection": {"detector_id": "yolo"}},
    )
    await orchestrator.bus.publish("event.gif.ready", gif2)
    await asyncio.sleep(0.1)  # Give time for handler to run and suppress

    # First clip
    clip1 = MediaArtifact(
        camera_id="camera1",
        artifact_path=str(clip_path),
        media_kind="clip",
        content_type="video/mp4",
        metadata={"detection": {"detector_id": "yolo"}},
    )
    sender.sent.clear()
    await orchestrator.bus.publish("event.clip.ready", clip1)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)

    # Second clip within cooldown - should be suppressed
    clock.advance(10.0)
    sender.sent.clear()
    clip2 = MediaArtifact(
        camera_id="camera1",
        artifact_path=str(clip_path),
        media_kind="clip",
        content_type="video/mp4",
        metadata={"detection": {"detector_id": "yolo"}},
    )
    await orchestrator.bus.publish("event.clip.ready", clip2)
    await asyncio.sleep(0.1)  # Give time for handler to run and suppress

    await orchestrator.stop()

    # Should have 3 notifications total (one of each type)
    assert len(sender.photos) == 1, "Expected 1 snapshot"
    assert len(sender.animations) == 1, "Expected 1 GIF"
    assert len(sender.videos) == 1, "Expected 1 clip"
