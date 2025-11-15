"""Tests for the Dynaconf-backed configuration service."""

from __future__ import annotations

import pytest

from spyoncino.core.config import ConfigService, ConfigSnapshot


def test_config_service_loads_snapshot(sample_config_service: ConfigService) -> None:
    snapshot = sample_config_service.snapshot
    assert isinstance(snapshot, ConfigSnapshot)
    assert snapshot.camera.camera_id == "lab"
    assert snapshot.storage.snapshot_dir.exists()


def test_module_config_generation(sample_config_service: ConfigService) -> None:
    camera_cfg = sample_config_service.module_config_for("modules.input.camera_simulator")
    assert camera_cfg.options["camera_id"] == "lab"
    assert camera_cfg.options["frame_width"] == 64

    snapshot_cfg = sample_config_service.module_config_for("modules.event.snapshot_writer")
    assert snapshot_cfg.options["frame_topics"] == ["camera.lab.frame"]

    telegram_cfg = sample_config_service.module_config_for("modules.output.telegram_notifier")
    assert telegram_cfg.options["token"] == "123:ABC"
    assert telegram_cfg.options["chat_id"] == 654321

    clip_cfg = sample_config_service.module_config_for("modules.event.clip_builder")
    assert clip_cfg.options["enabled"] is True
    assert clip_cfg.options["output_topic"] == "event.clip.ready"

    zoning_cfg = sample_config_service.module_config_for("modules.process.zoning_filter")
    assert zoning_cfg.options["enabled"] is True
    assert zoning_cfg.options["zones"]

    yolo_cfg = sample_config_service.module_config_for("modules.process.yolo_detector")
    assert yolo_cfg.options["alert_labels"] == ["person"]

    control_api_cfg = sample_config_service.module_config_for("modules.dashboard.control_api")
    assert control_api_cfg.options["serve_api"] is False

    telegram_bot_cfg = sample_config_service.module_config_for("modules.dashboard.telegram_bot")
    assert telegram_bot_cfg.options["default_camera_id"] == "lab"
    assert telegram_bot_cfg.options["user_whitelist"] == [42]

    alert_cfg = sample_config_service.module_config_for("modules.process.detection_event_router")
    assert alert_cfg.options["input_topic"] == "process.yolo.detected"
    assert alert_cfg.options["target_labels"] == ["person"]

    storage_cfg = sample_config_service.module_config_for("modules.storage.retention")
    assert storage_cfg.options["root_dir"]
    assert storage_cfg.options["stats_topic"] == "storage.stats"

    analytics_cfg = sample_config_service.module_config_for("modules.analytics.event_logger")
    assert analytics_cfg.options["detection_topics"]
    assert analytics_cfg.options["storage_topic"] == "storage.stats"


def test_unknown_module_raises_error(sample_config_service: ConfigService) -> None:
    with pytest.raises(KeyError):
        sample_config_service.module_config_for("modules.unknown")


def test_apply_changes_updates_snapshot(sample_config_service: ConfigService) -> None:
    snapshot = sample_config_service.apply_changes({"zoning": {"drop_outside": True}})
    assert snapshot.zoning.drop_outside is True
