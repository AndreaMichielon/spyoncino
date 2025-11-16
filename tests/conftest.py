from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from spyoncino.core.config import ConfigService


def _write_yaml(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


@pytest.fixture
def sample_config_dir(tmp_path: Path) -> Path:
    """
    Provide a temporary configuration directory for tests.
    """

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    recordings_dir = tmp_path / "recordings"
    config_yaml = f"""
    cameras:
      - camera_id: "lab"
        interval_seconds: 0.01
        rtsp_url: "rtsp://example.test/stream"
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
      notifications:
        output_for_motion: "text"
        output_for_detection: "gif"
        gif:
          duration_seconds: 2
          fps: 8
          max_frames: 8
        video:
          duration_seconds: 0.5
          fps: 4
          max_file_size_mb: 25
      media_artifacts:
        enabled: true
        detection_topic: "process.motion.unique"
        output_topic: "event.clip.ready"
        duration_seconds: 0.5
        fps: 4
        max_artifacts: 2
      zoning:
        enabled: true
        input_topic: "process.motion.unique"
        output_topic: "process.motion.zoned"
        frame_width: 64
        frame_height: 48
        zones:
          - camera_id: "lab"
            zone_id: "door"
            bounds: [0.0, 0.0, 0.6, 1.0]
            labels: ["person"]

    storage:
      path: "{recordings_dir.as_posix()}"
      snap_subdir: "snapshots"
      gif_subdir: "gifs"
      video_subdir: "clips"
      s3:
        enabled: true
        bucket: "lab-bucket"
        region_name: "eu-west-1"
        prefix: "lab"

    system:
      security:
        notification_chat_id: 123456
        allow_group_commands: false
        silent_unauthorized: true
      rate_limiting:
        notification_rate_limit: 2
        command_rate_limit: 3
        failed_auth_lockout: 100
      behavior:
        send_typing_action: true
        delete_old_menus: true
        command_timeout: 15
        snapshot_timeout: 5
      resilience:
        enabled: true
        scenarios:
          - name: "delay-alerts"
            topic: "process.alert.detected"
            latency_ms: 25
            drop_probability: 0.05
      control_api: &control_api_lab
        host: "127.0.0.1"
        port: 9000
        serve_api: false
        command_topic: "dashboard.control.command"
        config_topic: "config.update"

    dashboards:
      websocket_gateway:
        host: "127.0.0.1"
        port: 8800
        serve_http: false
        topics:
          - "status.health.summary"
          - "storage.stats"
      modules:
        - module: "modules.dashboard.control_api"
        - module: "modules.dashboard.websocket_gateway"
        - module: "modules.dashboard.telegram_bot"
          options:
            token: "@secrets telegram.token"
            default_camera_id: "lab"
            setup_password: "@secrets authentication.setup_password"
            superuser_id: "@secrets authentication.superuser_id"
            user_whitelist:
              - 42

    outputs:
      rate_limit:
        input_topic: "event.snapshot.ready"
        output_topic: "event.snapshot.allowed"
        max_events: 10
        per_seconds: 60
        key_field: "camera_id"
      modules:
        - module: "modules.output.telegram_notifier"
          additive: true
          options:
            token: "@secrets telegram.token"
            chat_id: "@secrets telegram.chat_id"
            read_timeout: 5
            write_timeout: 7
        - module: "modules.output.telegram_notifier"
          additive: true
          options:
            token: "@secrets telegram.token"
            chat_id: 777777
            send_typing_action: false

    """
    secrets_yaml = """
    telegram:
      token: "123:ABC"
      chat_id: 654321

    authentication:
      setup_password: "example"
      superuser_id: 42
      user_whitelist:
        - 42
    """
    _write_yaml(config_dir / "config.yaml", config_yaml)
    _write_yaml(config_dir / "secrets.yaml", secrets_yaml)
    return config_dir


@pytest.fixture
def sample_config_service(sample_config_dir: Path) -> ConfigService:
    """Return a ConfigService wired to the temporary configuration."""

    return ConfigService(config_dir=sample_config_dir)
