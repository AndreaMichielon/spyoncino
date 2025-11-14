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
    camera:
      camera_id: "lab"
      interval_seconds: 0.01
      rtsp_url: "rtsp://example.test/stream"
      width: 64
      height: 48

    storage:
      path: "{recordings_dir.as_posix()}"
      snapshot_subdir: "snapshots"

    notifications:
      gif_for_motion: false
      gif_for_person: true

    advanced:
      telegram_read_timeout: 5
      telegram_write_timeout: 7
    """
    telegram_yaml = """
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
    """
    secrets_yaml = """
    telegram:
      token: "123:ABC"
      chat_id: 654321

    authentication:
      setup_password: "example"
    """
    _write_yaml(config_dir / "config.yaml", config_yaml)
    _write_yaml(config_dir / "telegram.yaml", telegram_yaml)
    _write_yaml(config_dir / "secrets.yaml", secrets_yaml)
    return config_dir


@pytest.fixture
def sample_config_service(sample_config_dir: Path) -> ConfigService:
    """Return a ConfigService wired to the temporary configuration."""

    return ConfigService(config_dir=sample_config_dir)
