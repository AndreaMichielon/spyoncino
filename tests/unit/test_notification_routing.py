"""Tests for notification routing based on output_for_motion and output_for_detection."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from spyoncino.core.config import ConfigService


def _write_yaml(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _create_test_config(
    tmp_path: Path, output_for_motion: str, output_for_detection: str
) -> ConfigService:
    """Create a test config with specified notification preferences."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    recordings_dir = tmp_path / "recordings"

    config_yaml = f"""
    cameras:
      - camera_id: "test"
        interval_seconds: 0.01
        width: 64
        height: 48

    detection:
      interval: 0.5
      confidence: 0.4
      alert_labels: ["person"]
      dedupe:
        input_topic: "process.motion.detected"
        output_topic: "process.motion.unique"
        window_seconds: 0.5
      alert_pipeline:
        input_topic: "process.yolo.detected"
        output_topic: "process.alert.detected"
        target_labels: ["person"]
      notifications:
        output_for_motion: "{output_for_motion}"
        output_for_detection: "{output_for_detection}"
        video:
          enabled: true
          fps: 12
          duration_seconds: 5.0
          max_artifacts: 25

    storage:
      path: "{recordings_dir.as_posix()}"

    system:
      security:
        notification_chat_id: 123456
    """

    secrets_yaml = """
    telegram:
      token: "test:token"
      chat_id: 654321
    """

    _write_yaml(config_dir / "config.yaml", config_yaml)
    _write_yaml(config_dir / "secrets.yaml", secrets_yaml)
    return ConfigService(config_dir=config_dir)


@pytest.mark.parametrize(
    "output_for_motion,output_for_detection,expected_snapshot_topic,expected_gif_topic,expected_clip_topic,expected_clip_enabled",
    [
        # Motion = snap, Detection = snap
        ("snap", "snap", "event.snapshot.allowed", None, None, False),
        # Motion = snap, Detection = gif
        ("snap", "gif", "event.snapshot.allowed", "event.gif.ready", None, False),
        # Motion = snap, Detection = video
        ("snap", "video", "event.snapshot.allowed", None, "event.clip.ready", True),
        # Motion = gif, Detection = snap
        ("gif", "snap", None, "event.gif.ready", None, False),
        # Motion = gif, Detection = gif
        ("gif", "gif", None, "event.gif.ready", None, False),
        # Motion = gif, Detection = video
        ("gif", "video", None, "event.gif.ready", "event.clip.ready", True),
        # Motion = video, Detection = snap
        ("video", "snap", None, None, "event.clip.ready", True),
        # Motion = video, Detection = gif
        ("video", "gif", None, "event.gif.ready", "event.clip.ready", True),
        # Motion = video, Detection = video
        ("video", "video", None, None, "event.clip.ready", True),
        # Motion = text, Detection = snap
        ("text", "snap", "event.snapshot.allowed", None, None, False),
        # Motion = text, Detection = gif
        ("text", "gif", "event.snapshot.allowed", "event.gif.ready", None, False),
        # Motion = text, Detection = video
        ("text", "video", "event.snapshot.allowed", None, "event.clip.ready", True),
        # Motion = none, Detection = snap
        ("none", "snap", None, None, None, False),
        # Motion = none, Detection = gif
        ("none", "gif", None, "event.gif.ready", None, False),
        # Motion = none, Detection = video
        ("none", "video", None, None, "event.clip.ready", True),
    ],
)
def test_notification_routing_combinations(
    tmp_path: Path,
    output_for_motion: str,
    output_for_detection: str,
    expected_snapshot_topic: str | None,
    expected_gif_topic: str | None,
    expected_clip_topic: str | None,
    expected_clip_enabled: bool,
) -> None:
    """Test all combinations of output_for_motion and output_for_detection."""
    service = _create_test_config(tmp_path, output_for_motion, output_for_detection)

    # Check TelegramNotifier configuration
    telegram_cfg = service.module_config_for("modules.output.telegram_notifier")
    assert telegram_cfg.options.get("topic") == expected_snapshot_topic
    assert telegram_cfg.options.get("gif_topic") == expected_gif_topic
    assert telegram_cfg.options.get("clip_topic") == expected_clip_topic

    # Check ClipBuilder configuration
    clip_cfg = service.module_config_for("modules.event.clip_builder")
    assert clip_cfg.options.get("enabled") == expected_clip_enabled
    if expected_clip_enabled:
        # If clips are enabled, verify detection_topic is wired correctly
        if output_for_detection == "video":
            # Should listen to alert pipeline (detection alerts)
            assert clip_cfg.options.get("detection_topic") == "process.alert.detected"
        elif output_for_motion == "video":
            # Should listen to dedupe (motion events)
            assert clip_cfg.options.get("detection_topic") == "process.motion.unique"

    # Check GifBuilder configuration
    gif_cfg = service.module_config_for("modules.event.gif_builder")
    if expected_gif_topic:
        # GIF builder should be enabled if GIF is wanted
        assert gif_cfg.enabled is True
        # Verify detection_topic is wired correctly
        if output_for_detection == "gif":
            # Should listen to alert pipeline (detection alerts)
            assert gif_cfg.options.get("detection_topic") == "process.alert.detected"
        elif output_for_motion == "gif":
            # Should listen to dedupe (motion events)
            assert gif_cfg.options.get("detection_topic") == "process.motion.unique"
    else:
        # GIF builder should be disabled if GIF is not wanted
        assert gif_cfg.enabled is False

    # Check SnapshotWriter configuration
    snapshot_cfg = service.module_config_for("modules.event.snapshot_writer")
    # Snapshot writer is always enabled, but rate limiter controls what gets through
    assert snapshot_cfg.enabled is True


def test_notification_delivery_modes(tmp_path: Path) -> None:
    """Test that delivery modes are set correctly based on preferences."""
    # Test text mode
    service = _create_test_config(tmp_path, "text", "snap")
    telegram_cfg = service.module_config_for("modules.output.telegram_notifier")
    assert telegram_cfg.options.get("snapshot_delivery") == "text"

    # Test photo mode
    service = _create_test_config(tmp_path, "snap", "gif")
    telegram_cfg = service.module_config_for("modules.output.telegram_notifier")
    assert telegram_cfg.options.get("snapshot_delivery") == "photo"

    # Test animation mode (for GIFs)
    service = _create_test_config(tmp_path, "gif", "video")
    telegram_cfg = service.module_config_for("modules.output.telegram_notifier")
    # GIF topic should be set
    assert telegram_cfg.options.get("gif_topic") == "event.gif.ready"

    # Test video mode (for clips)
    service = _create_test_config(tmp_path, "video", "video")
    telegram_cfg = service.module_config_for("modules.output.telegram_notifier")
    # Clip topic should be set
    assert telegram_cfg.options.get("clip_topic") == "event.clip.ready"
