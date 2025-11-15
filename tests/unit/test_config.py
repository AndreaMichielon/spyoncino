"""Tests for the Dynaconf-backed configuration service."""

from __future__ import annotations

from pathlib import Path

import pytest

from spyoncino.core.config import (
    AuthenticationSettings,
    CameraSettings,
    ConfigError,
    ConfigService,
    ConfigSnapshot,
    StorageSettings,
    TelegramSecrets,
    TelegramSecuritySettings,
    ZoningSettings,
)
from spyoncino.core.contracts import ModuleConfig


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
    assert telegram_cfg.options["chat_targets"] == [654321, 123456, 42]
    assert telegram_cfg.options["topic"] == "event.snapshot.allowed"

    clip_cfg = sample_config_service.module_config_for("modules.event.clip_builder")
    assert clip_cfg.options["enabled"] is True
    assert clip_cfg.options["output_topic"] == "event.clip.ready"
    assert clip_cfg.options["frame_topics"] == ["camera.lab.frame"]

    zoning_cfg = sample_config_service.module_config_for("modules.process.zoning_filter")
    assert zoning_cfg.options["enabled"] is True
    assert zoning_cfg.options["zones"]
    assert zoning_cfg.options["camera_dimensions"]["lab"] == {"width": 64, "height": 48}

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


def test_zoning_settings_accepts_grouped_definitions(sample_config_service: ConfigService) -> None:
    snapshot = sample_config_service.apply_changes(
        {
            "zoning": {
                "zones": [
                    {
                        "camera_id": "garage",
                        "zones": {
                            "door": {
                                "bounds": [0.0, 0.0, 0.5, 0.5],
                                "labels": ["person"],
                                "action": "include",
                            },
                            "outside": [0.5, 0.5, 1.0, 1.0],
                        },
                    }
                ]
            }
        }
    )
    assert len(snapshot.zoning.zones) == 2
    zone_ids = sorted(zone.zone_id for zone in snapshot.zoning.zones)
    assert zone_ids == ["door", "outside"]
    assert snapshot.zoning.zones[1].bounds == (0.5, 0.5, 1.0, 1.0)


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


def test_usb_camera_config_handles_string_and_missing_ports(
    sample_config_service: ConfigService,
) -> None:
    snapshot = sample_config_service.apply_changes(
        {
            "cameras": [
                {
                    "camera_id": "lab",
                    "usb_port": "/dev/video0",
                    "width": 64,
                    "height": 48,
                },
                {
                    "camera_id": "garage",
                    "usb_port": 2,
                    "width": 128,
                    "height": 72,
                },
            ]
        }
    )
    configs = snapshot.module_configs("modules.input.usb_camera")
    assert len(configs) == 2

    options_by_camera = {cfg.options["camera_id"]: cfg.options for cfg in configs}
    assert options_by_camera["lab"]["device_path"] == "/dev/video0"
    assert "device_index" not in options_by_camera["lab"]
    assert options_by_camera["garage"]["device_index"] == 2


def test_rtsp_camera_config_filters_missing_urls(sample_config_service: ConfigService) -> None:
    snapshot = sample_config_service.apply_changes(
        {
            "cameras": [
                {
                    "camera_id": "lab",
                    "rtsp_url": "rtsp://example.test/lab",
                },
                {
                    "camera_id": "garage",
                    "rtsp_url": None,
                },
            ]
        }
    )
    configs = snapshot.module_configs("modules.input.rtsp_camera")
    assert [cfg.options["camera_id"] for cfg in configs] == ["lab"]
    assert configs[0].options["rtsp_url"] == "rtsp://example.test/lab"


def test_zoning_flatten_handles_dict_and_inline_definitions() -> None:
    dict_based = {
        "garage": {
            "driveway": {
                "bounds": [0.0, 0.0, 0.5, 0.5],
                "labels": ["car"],
            },
            "porch": [0.5, 0.5, 1.0, 1.0],
        }
    }
    list_based = [
        {
            "camera_id": "lab",
            "zones": {
                "door": [0.0, 0.0, 0.4, 0.4],
            },
        },
        {
            "camera_id": "yard",
            "zone_id": "garden",
            "bounds": [0.1, 0.1, 0.9, 0.9],
        },
    ]

    dict_result = ZoningSettings._flatten_zone_definitions(dict_based)
    list_result = ZoningSettings._flatten_zone_definitions(list_based)

    dict_zone_ids = sorted(zone["zone_id"] for zone in dict_result)
    list_zone_ids = sorted(zone["zone_id"] for zone in list_result)

    assert dict_zone_ids == ["driveway", "porch"]
    assert list_zone_ids == ["door", "garden"]
    assert dict_result[0]["camera_id"] == "garage"


def test_manifest_entry_requires_token_and_chat_id() -> None:
    snapshot = ConfigSnapshot()
    config = ModuleConfig(options={})
    with pytest.raises(ConfigError):
        snapshot._apply_manifest_defaults("modules.output.telegram_notifier", config)


def test_manifest_defaults_fill_token_and_chat_id_from_snapshot() -> None:
    snapshot = ConfigSnapshot(
        telegram=TelegramSecrets(token="123:ABC", chat_id=None),
        telegram_security=TelegramSecuritySettings(notification_chat_id=42),
    )
    config = ModuleConfig(options={"chat_id": "@secrets telegram.chat_id"})
    result = snapshot._apply_manifest_defaults("modules.output.telegram_notifier", config)
    assert result.options["token"] == "123:ABC"
    assert result.options["chat_id"] == 42
    assert result.options["chat_targets"] == [42]
    assert result.options["topic"] == snapshot.rate_limit.output_topic


def test_manifest_defaults_use_superuser_when_chat_missing() -> None:
    snapshot = ConfigSnapshot(
        telegram=TelegramSecrets(token="123:ABC", chat_id=None),
        authentication=AuthenticationSettings(superuser_id=777, user_whitelist=[888]),
    )
    config = ModuleConfig(options={})
    result = snapshot._apply_manifest_defaults("modules.output.telegram_notifier", config)
    assert result.options["chat_id"] == 777
    assert result.options["chat_targets"] == [777, 888]


def test_manifest_defaults_use_whitelist_when_superuser_missing() -> None:
    snapshot = ConfigSnapshot(
        telegram=TelegramSecrets(token="123:ABC", chat_id=None),
        authentication=AuthenticationSettings(superuser_id=None, user_whitelist=[555, 666]),
    )
    config = ModuleConfig(options={})
    result = snapshot._apply_manifest_defaults("modules.output.telegram_notifier", config)
    assert result.options["chat_id"] == 555
    assert result.options["chat_targets"] == [555, 666]


def test_storage_settings_ensure_directories(tmp_path: Path) -> None:
    storage = StorageSettings(
        path=tmp_path / "recordings",
        snap_subdir="snaps",
        gif_subdir="gifs",
        video_subdir="clips",
    )
    storage.ensure_directories()

    assert storage.path.exists()
    assert storage.snapshot_dir.exists()
    assert storage.gif_dir.exists()
    assert storage.clip_dir.exists()
