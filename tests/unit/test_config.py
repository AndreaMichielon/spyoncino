"""Tests for the Dynaconf-backed configuration service."""

from __future__ import annotations

import pytest

from spyoncino.core.config import CameraSettings, ConfigService, ConfigSnapshot


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
    assert telegram_cfg.options["topic"] == "event.snapshot.allowed"

    clip_cfg = sample_config_service.module_config_for("modules.event.clip_builder")
    assert clip_cfg.options["enabled"] is True
    assert clip_cfg.options["output_topic"] == "event.clip.ready"
    assert clip_cfg.options["frame_topics"] == ["camera.lab.frame"]

    zoning_cfg = sample_config_service.module_config_for("modules.process.zoning_filter")
    assert zoning_cfg.options["enabled"] is True
    assert zoning_cfg.options["zones"]

    yolo_cfg = sample_config_service.module_config_for("modules.process.yolo_detector")
    assert yolo_cfg.options["alert_labels"] == ["person"]
    assert yolo_cfg.options["input_topics"] == ["camera.lab.frame"]

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

    db_logger_cfg = sample_config_service.module_config_for("modules.analytics.db_logger")
    assert db_logger_cfg.options["database_url"].startswith("sqlite:///")
    assert "process.motion.unique" in db_logger_cfg.options["topics"]

    s3_cfg = sample_config_service.module_config_for("modules.storage.s3_uploader")
    assert s3_cfg.options["bucket"] == "lab-bucket"
    assert s3_cfg.options["enabled"] is True

    ws_cfg = sample_config_service.module_config_for("modules.dashboard.websocket_gateway")
    assert ws_cfg.options["serve_http"] is False
    assert "storage.stats" in ws_cfg.options["topics"]

    resilience_cfg = sample_config_service.module_config_for("modules.status.resilience_tester")
    assert resilience_cfg.options["enabled"] is True
    assert resilience_cfg.options["scenarios"][0]["name"] == "delay-alerts"


def test_unknown_module_raises_error(sample_config_service: ConfigService) -> None:
    with pytest.raises(KeyError):
        sample_config_service.module_config_for("modules.unknown")


def test_apply_changes_updates_snapshot(sample_config_service: ConfigService) -> None:
    snapshot = sample_config_service.apply_changes({"zoning": {"drop_outside": True}})
    assert snapshot.zoning.drop_outside is True


def test_camera_settings_allows_null_dimensions() -> None:
    settings = CameraSettings(width=None, height=None, fps=None)
    width, height = settings.resolved_dimensions()
    assert width == CameraSettings.DEFAULT_WIDTH
    assert height == CameraSettings.DEFAULT_HEIGHT
    assert settings.fps is None


def test_motion_detector_receives_all_camera_topics(sample_config_service: ConfigService) -> None:
    motion_cfg = sample_config_service.module_config_for("modules.process.motion_detector")
    assert motion_cfg.options["input_topics"] == ["camera.lab.frame"]
    assert motion_cfg.options["input_topic"] == "camera.lab.frame"


def test_manifest_multi_instance_outputs(sample_config_service: ConfigService) -> None:
    configs = sample_config_service.module_configs_for("modules.output.telegram_notifier")
    assert len(configs) == 2
    chat_ids = sorted(cfg.options["chat_id"] for cfg in configs)
    assert chat_ids == [654321, 777777]


def test_camera_simulator_config_falls_back_when_width_missing(
    sample_config_service: ConfigService,
) -> None:
    snapshot = sample_config_service.apply_changes(
        {"cameras": [{"camera_id": "lab", "width": None}]}
    )
    module_cfg = snapshot.module_config("modules.input.camera_simulator")
    assert module_cfg.options["frame_width"] == CameraSettings.DEFAULT_WIDTH
    assert module_cfg.options["frame_height"] == snapshot.camera.height


def test_multiple_camera_configs_are_exposed(sample_config_service: ConfigService) -> None:
    snapshot = sample_config_service.apply_changes(
        {
            "cameras": [
                {
                    "camera_id": "lab",
                    "usb_port": 0,
                    "width": 64,
                    "height": 48,
                },
                {
                    "camera_id": "garage",
                    "usb_port": 1,
                    "width": 128,
                    "height": 96,
                },
            ]
        }
    )
    configs = snapshot.module_configs("modules.input.camera_simulator")
    assert len(configs) == 2
    assert configs[0].options["camera_id"] == "lab"
    assert configs[1].options["camera_id"] == "garage"


def test_module_configs_for_service_wrapper(sample_config_service: ConfigService) -> None:
    sample_config_service.apply_changes(
        {
            "cameras": [
                {
                    "camera_id": "lab",
                    "usb_port": 0,
                },
                {
                    "camera_id": "garage",
                    "usb_port": 2,
                },
            ]
        }
    )
    configs = sample_config_service.module_configs_for("modules.input.usb_camera")
    camera_ids = [cfg.options["camera_id"] for cfg in configs]
    assert camera_ids == ["lab", "garage"]
