"""
Dynaconf-powered configuration loader with Pydantic validation.

The configuration service is responsible for loading the layered YAML
configuration files described in `TODO_ARCHITECTURE.md`, validating them, and
producing module-friendly `ModuleConfig` instances so the orchestrator can wire
modules without hand-written dictionaries.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from dynaconf import Dynaconf
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .contracts import BaseModule, ModuleConfig


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """Case-insensitive dictionary lookup helper."""
    value = raw.get(key) or raw.get(key.upper()) or raw.get(key.lower())
    if isinstance(value, dict):
        return value
    return {}


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge dictionaries without mutating the originals."""
    result: dict[str, Any] = {**base}
    for key, value in updates.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


CONFIG_FILENAMES = ("config.yaml", "telegram.yaml", "secrets.yaml")
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_DIR = _REPO_ROOT / "config"


class ConfigError(RuntimeError):
    """Raised when configuration files are missing or invalid."""


class CameraSettings(BaseModel):
    """User-configurable camera parameters."""

    model_config = ConfigDict(extra="ignore")

    camera_id: str = Field(default="default")
    usb_port: int | str = Field(default=0)
    rtsp_url: str | None = Field(default=None)
    interval_seconds: float = Field(default=1.0)
    width: int = Field(default=640)
    height: int = Field(default=480)
    fps: int = Field(default=15)

    @property
    def topic(self) -> str:
        return f"camera.{self.camera_id}.frame"


class DetectionSettings(BaseModel):
    """Subset of detection options required for Week 2 features."""

    model_config = ConfigDict(extra="ignore")

    interval: float = Field(default=2.0)
    motion_threshold: int = Field(default=5)
    confidence: float = Field(default=0.25)
    model_path: str | None = Field(default=None)
    class_filter: list[str] = Field(default_factory=list)
    person_cooldown_seconds: float = Field(default=30.0)
    bbox_overlap_threshold: float = Field(default=0.6)
    alert_labels: list[str] = Field(default_factory=lambda: ["person"])


class AlertPipelineSettings(BaseModel):
    """Settings for the detection router that enforces anti-spam rules."""

    model_config = ConfigDict(extra="ignore")

    input_topic: str = Field(default="process.yolo.detected")
    output_topic: str = Field(default="process.alert.detected")
    target_labels: list[str] = Field(default_factory=lambda: ["person"])
    min_confidence: float = Field(default=0.35)
    cooldown_seconds: float = Field(default=30.0)
    bbox_iou_threshold: float = Field(default=0.6)
    timeout_seconds: float = Field(default=5.0)


class DedupeSettings(BaseModel):
    """Configuration for detection deduplication."""

    model_config = ConfigDict(extra="ignore")

    input_topic: str = Field(default="process.motion.detected")
    output_topic: str = Field(default="process.motion.unique")
    window_seconds: float = Field(default=2.0)
    key_fields: list[str] = Field(
        default_factory=lambda: ["camera_id", "detector_id", "attributes.label"]
    )


class StorageSettings(BaseModel):
    """File-system persistence configuration."""

    model_config = ConfigDict(extra="ignore")

    path: Path = Field(default_factory=lambda: _REPO_ROOT / "recordings")
    retention_hours: int = Field(default=24)
    aggressive_cleanup_hours: int = Field(default=12)
    low_space_threshold_gb: float = Field(default=1.0, ge=0.0)
    cleanup_interval_seconds: float = Field(default=600.0)
    stats_topic: str = Field(default="storage.stats")
    artifact_globs: list[str] = Field(
        default_factory=lambda: [
            "snapshots/*.png",
            "snapshots/*.jpg",
            "gifs/*.gif",
            "clips/*.mp4",
        ]
    )
    snapshot_subdir: str = Field(default="snapshots")
    gif_subdir: str = Field(default="gifs")
    clip_subdir: str = Field(default="clips")

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path(cls, value: Any) -> Path:
        if isinstance(value, Path):
            return value
        return Path(value)

    @field_validator("cleanup_interval_seconds")
    @classmethod
    def _positive_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("cleanup_interval_seconds must be positive")
        return value

    @property
    def snapshot_dir(self) -> Path:
        return self.path / self.snapshot_subdir

    @property
    def gif_dir(self) -> Path:
        return self.path / self.gif_subdir

    @property
    def clip_dir(self) -> Path:
        return self.path / self.clip_subdir

    def ensure_directories(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.gif_dir.mkdir(parents=True, exist_ok=True)
        self.clip_dir.mkdir(parents=True, exist_ok=True)


class NotificationSettings(BaseModel):
    """Notification preferences (GIFs, etc.)."""

    model_config = ConfigDict(extra="ignore")

    gif_for_motion: bool = Field(default=False)
    gif_for_person: bool = Field(default=True)
    gif_duration: float = Field(default=3.0)
    gif_fps: int = Field(default=10)
    notification_gif_fps: int = Field(default=10)
    max_gif_frames: int = Field(default=20)
    max_file_size_mb: float = Field(default=50.0)


class RateLimitSettings(BaseModel):
    """Throttle outgoing notifications."""

    model_config = ConfigDict(extra="ignore")

    input_topic: str = Field(default="event.snapshot.ready")
    output_topic: str = Field(default="event.snapshot.allowed")
    max_events: int = Field(default=5)
    per_seconds: float = Field(default=60.0)
    key_field: str = Field(default="camera_id")


class ZoneDefinition(BaseModel):
    """Single zone definition applied to detections."""

    model_config = ConfigDict(extra="ignore")

    camera_id: str = Field(default="default")
    zone_id: str = Field(description="Stable identifier for the zone.")
    name: str | None = Field(default=None)
    bounds: tuple[float, float, float, float] = Field(
        default=(0.0, 0.0, 1.0, 1.0),
        description="Normalized (x1, y1, x2, y2) bounds.",
    )
    labels: list[str] = Field(default_factory=list)
    action: Literal["include", "exclude"] = Field(default="include")
    frame_width: int | None = None
    frame_height: int | None = None

    @field_validator("bounds")
    @classmethod
    def _validate_bounds(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bounds must contain four float values")
        x1, y1, x2, y2 = value
        if not (0.0 <= x1 <= 1.0 and 0.0 <= y1 <= 1.0 and 0.0 <= x2 <= 1.0 and 0.0 <= y2 <= 1.0):
            raise ValueError("bounds must be normalized between 0 and 1")
        if x2 <= x1 or y2 <= y1:
            raise ValueError("bounds must describe a positive area")
        return value


class ZoningSettings(BaseModel):
    """Configuration for the zoning processor."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=False)
    input_topic: str = Field(default="process.motion.unique")
    output_topic: str = Field(default="process.motion.zoned")
    unmatched_topic: str | None = Field(
        default=None, description="Optional topic for detections that miss all zones."
    )
    drop_outside: bool = Field(
        default=False, description="When true, detections outside include zones are dropped."
    )
    frame_width: int = Field(default=640)
    frame_height: int = Field(default=480)
    zones: list[ZoneDefinition] = Field(default_factory=list)


class ClipSettings(BaseModel):
    """Configuration for MP4 clip generation."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=False)
    detection_topic: str = Field(default="process.motion.unique")
    output_topic: str = Field(default="event.clip.ready")
    duration_seconds: float = Field(default=5.0)
    fps: int = Field(default=12)
    max_artifacts: int | None = Field(default=25)


class ControlApiSettings(BaseModel):
    """Control API (FastAPI) configuration."""

    model_config = ConfigDict(extra="ignore")

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8080)
    serve_api: bool = Field(default=True)
    command_topic: str = Field(default="dashboard.control.command")
    config_topic: str = Field(default="config.update")


class AdvancedSettings(BaseModel):
    """Advanced tuning knobs used by multiple modules."""

    model_config = ConfigDict(extra="ignore")

    telegram_read_timeout: float = Field(default=30.0)
    telegram_write_timeout: float = Field(default=60.0)
    analytics_figure_width: float = Field(default=22.0)
    analytics_figure_height: float = Field(default=5.5)
    analytics_intervals: list[int] = Field(default_factory=lambda: [5, 15, 60, 120])


class AnalyticsSettings(BaseModel):
    """Configuration for the analytics/event logging module."""

    model_config = ConfigDict(extra="ignore")

    db_filename: str = Field(default="events.db")
    detection_topics: list[str] = Field(default_factory=lambda: ["process.motion.unique"])
    alert_topics: list[str] = Field(default_factory=lambda: ["process.alert.detected"])
    storage_topic: str = Field(default="storage.stats")


class TelegramSecrets(BaseModel):
    """Token + chat identifiers loaded from secrets/environment."""

    model_config = ConfigDict(extra="ignore")

    token: str | None = Field(default=None)
    chat_id: int | None = Field(default=None)


class TelegramSecuritySettings(BaseModel):
    """Access control rules for Telegram notifications."""

    model_config = ConfigDict(extra="ignore")

    notification_chat_id: int | None = Field(default=None)
    allow_group_commands: bool = Field(default=True)
    silent_unauthorized: bool = Field(default=True)


class TelegramBehaviorSettings(BaseModel):
    """Bot UX preferences."""

    model_config = ConfigDict(extra="ignore")

    send_typing_action: bool = Field(default=True)
    delete_old_menus: bool = Field(default=True)
    command_timeout: int = Field(default=30)
    snapshot_timeout: int = Field(default=10)


class TelegramRateLimitSettings(BaseModel):
    """Throttle configuration for notifications and commands."""

    model_config = ConfigDict(extra="ignore")

    notification_rate_limit: int = Field(default=5)
    command_rate_limit: int = Field(default=10)
    failed_auth_lockout: int = Field(default=300)


class AuthenticationSettings(BaseModel):
    """Authentication and authorization settings for control surfaces."""

    model_config = ConfigDict(extra="ignore")

    setup_password: str | None = Field(default=None)
    superuser_id: int | None = Field(default=None)
    user_whitelist: list[int] = Field(default_factory=list)

    @field_validator("user_whitelist", mode="before")
    @classmethod
    def _coerce_user_ids(cls, value: Any) -> list[int]:
        if value is None:
            return []
        if isinstance(value, list | tuple | set):
            result: list[int] = []
            for item in value:
                try:
                    result.append(int(item))
                except (TypeError, ValueError):
                    continue
            return result
        try:
            return [int(value)]
        except (TypeError, ValueError):
            return []


class ConfigSnapshot(BaseModel):
    """
    Validated, strongly typed view of the merged configuration.

    Provides helpers to derive per-module configuration dictionaries.
    """

    model_config = ConfigDict(extra="ignore")

    camera: CameraSettings = Field(default_factory=CameraSettings)
    detection: DetectionSettings = Field(default_factory=DetectionSettings)
    alert_pipeline: AlertPipelineSettings = Field(default_factory=AlertPipelineSettings)
    dedupe: DedupeSettings = Field(default_factory=DedupeSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    zoning: ZoningSettings = Field(default_factory=ZoningSettings)
    clip: ClipSettings = Field(default_factory=ClipSettings)
    advanced: AdvancedSettings = Field(default_factory=AdvancedSettings)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)
    control_api: ControlApiSettings = Field(default_factory=ControlApiSettings)
    telegram: TelegramSecrets = Field(default_factory=TelegramSecrets)
    telegram_security: TelegramSecuritySettings = Field(default_factory=TelegramSecuritySettings)
    telegram_behavior: TelegramBehaviorSettings = Field(default_factory=TelegramBehaviorSettings)
    telegram_rate_limiting: TelegramRateLimitSettings = Field(
        default_factory=TelegramRateLimitSettings
    )
    authentication: AuthenticationSettings = Field(default_factory=AuthenticationSettings)

    def module_config(self, module_name: str) -> ModuleConfig:
        """Produce a ModuleConfig tailored for the requested module."""

        def _camera_sim_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "camera_id": self.camera.camera_id,
                    "interval_seconds": self.camera.interval_seconds,
                    "frame_width": self.camera.width,
                    "frame_height": self.camera.height,
                }
            )

        def _usb_camera_config() -> ModuleConfig:
            port = self.camera.usb_port
            options: dict[str, Any] = {
                "camera_id": self.camera.camera_id,
                "fps": self.camera.fps,
                "frame_width": self.camera.width,
                "frame_height": self.camera.height,
            }
            if isinstance(port, int):
                options["device_index"] = port
            elif port is not None:
                options["device_path"] = str(port)
            return ModuleConfig(options=options)

        def _rtsp_camera_config() -> ModuleConfig:
            if not self.camera.rtsp_url:
                raise KeyError("camera.rtsp_url must be configured for RTSP module.")
            return ModuleConfig(
                options={
                    "camera_id": self.camera.camera_id,
                    "rtsp_url": self.camera.rtsp_url,
                    "fps": self.camera.fps,
                }
            )

        def _motion_detector_config() -> ModuleConfig:
            return ModuleConfig(options={"input_topic": self.camera.topic})

        def _yolo_detector_config() -> ModuleConfig:
            options: dict[str, Any] = {
                "input_topics": [self.camera.topic],
                "confidence_threshold": self.detection.confidence,
            }
            if self.detection.model_path:
                options["model_path"] = self.detection.model_path
            if self.detection.class_filter:
                options["class_filter"] = self.detection.class_filter
            if self.detection.alert_labels:
                options["alert_labels"] = self.detection.alert_labels
            return ModuleConfig(options=options)

        def _detection_router_config() -> ModuleConfig:
            target_labels = self.alert_pipeline.target_labels or self.detection.alert_labels
            return ModuleConfig(
                options={
                    "input_topic": self.alert_pipeline.input_topic,
                    "output_topic": self.alert_pipeline.output_topic,
                    "target_labels": target_labels or ["person"],
                    "min_confidence": self.alert_pipeline.min_confidence,
                    "cooldown_seconds": self.alert_pipeline.cooldown_seconds,
                    "bbox_iou_threshold": self.alert_pipeline.bbox_iou_threshold,
                    "timeout_seconds": self.alert_pipeline.timeout_seconds,
                }
            )

        def _snapshot_writer_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "frame_topics": [self.camera.topic],
                    "detection_topic": self.dedupe.output_topic,
                    "output_dir": str(self.storage.snapshot_dir),
                }
            )

        def _deduplicator_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "input_topic": self.dedupe.input_topic,
                    "output_topic": self.dedupe.output_topic,
                    "window_seconds": self.dedupe.window_seconds,
                    "key_fields": self.dedupe.key_fields,
                }
            )

        def _gif_builder_config() -> ModuleConfig:
            detection_topic = (
                self.alert_pipeline.output_topic
                if self.notifications.gif_for_person
                else self.dedupe.output_topic
            )
            return ModuleConfig(
                options={
                    "frame_topics": [self.camera.topic],
                    "detection_topic": detection_topic,
                    "output_dir": str(self.storage.gif_dir),
                    "fps": self.notifications.gif_fps,
                    "duration_seconds": self.notifications.gif_duration,
                    "max_frames": self.notifications.max_gif_frames,
                }
            )

        def _clip_builder_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "enabled": self.clip.enabled,
                    "frame_topics": [self.camera.topic],
                    "detection_topic": self.clip.detection_topic,
                    "output_topic": self.clip.output_topic,
                    "output_dir": str(self.storage.clip_dir),
                    "fps": self.clip.fps,
                    "duration_seconds": self.clip.duration_seconds,
                    "max_artifacts": self.clip.max_artifacts,
                }
            )

        def _zoning_filter_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "enabled": self.zoning.enabled,
                    "input_topic": self.zoning.input_topic,
                    "output_topic": self.zoning.output_topic,
                    "unmatched_topic": self.zoning.unmatched_topic,
                    "drop_outside": self.zoning.drop_outside,
                    "frame_width": self.zoning.frame_width,
                    "frame_height": self.zoning.frame_height,
                    "zones": [zone.model_dump() for zone in self.zoning.zones],
                }
            )

        def _control_api_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "host": self.control_api.host,
                    "port": self.control_api.port,
                    "serve_api": self.control_api.serve_api,
                    "command_topic": self.control_api.command_topic,
                    "config_topic": self.control_api.config_topic,
                }
            )

        def _telegram_control_bot_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "token": self.telegram.token,
                    "default_camera_id": self.camera.camera_id,
                    "command_topic": self.control_api.command_topic,
                    "health_topic": "status.health.summary",
                    "user_whitelist": self.authentication.user_whitelist,
                    "superuser_id": self.authentication.superuser_id,
                    "setup_password": self.authentication.setup_password,
                    "allow_group_commands": self.telegram_security.allow_group_commands,
                    "silent_unauthorized": self.telegram_security.silent_unauthorized,
                    "command_rate_limit": self.telegram_rate_limiting.command_rate_limit,
                }
            )

        def _rate_limiter_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "input_topic": self.rate_limit.input_topic,
                    "output_topic": self.rate_limit.output_topic,
                    "max_events": self.rate_limit.max_events,
                    "per_seconds": self.rate_limit.per_seconds,
                    "key_field": self.rate_limit.key_field,
                }
            )

        def _telegram_notifier_config() -> ModuleConfig:
            chat_id = self.telegram.chat_id or self.telegram_security.notification_chat_id
            return ModuleConfig(
                options={
                    "token": self.telegram.token,
                    "chat_id": chat_id,
                    "topic": self.rate_limit.output_topic,
                    "read_timeout": self.advanced.telegram_read_timeout,
                    "write_timeout": self.advanced.telegram_write_timeout,
                    "send_typing_action": self.telegram_behavior.send_typing_action,
                }
            )

        def _prometheus_exporter_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "port": 9093,
                    "addr": "127.0.0.1",
                    "bus_topic": "status.bus",
                }
            )

        def _storage_retention_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "root_dir": str(self.storage.path),
                    "retention_hours": self.storage.retention_hours,
                    "aggressive_hours": self.storage.aggressive_cleanup_hours,
                    "low_space_threshold_gb": self.storage.low_space_threshold_gb,
                    "cleanup_interval_seconds": self.storage.cleanup_interval_seconds,
                    "artifact_globs": self.storage.artifact_globs,
                    "stats_topic": self.storage.stats_topic,
                }
            )

        def _analytics_logger_config() -> ModuleConfig:
            detection_topics = self.analytics.detection_topics or [self.dedupe.output_topic]
            person_topics = self.analytics.alert_topics or [self.alert_pipeline.output_topic]
            db_path = self.storage.path / self.analytics.db_filename
            return ModuleConfig(
                options={
                    "db_path": str(db_path),
                    "detection_topics": detection_topics,
                    "person_topics": person_topics,
                    "storage_topic": self.analytics.storage_topic,
                    "figure_width": self.advanced.analytics_figure_width,
                    "figure_height": self.advanced.analytics_figure_height,
                    "analytics_intervals": self.advanced.analytics_intervals,
                }
            )

        builders: dict[str, Callable[[], ModuleConfig]] = {
            "modules.input.camera_simulator": _camera_sim_config,
            "modules.input.usb_camera": _usb_camera_config,
            "modules.input.rtsp_camera": _rtsp_camera_config,
            "modules.process.motion_detector": _motion_detector_config,
            "modules.process.yolo_detector": _yolo_detector_config,
            "modules.process.detection_event_router": _detection_router_config,
            "modules.event.deduplicator": _deduplicator_config,
            "modules.event.snapshot_writer": _snapshot_writer_config,
            "modules.event.gif_builder": _gif_builder_config,
            "modules.event.clip_builder": _clip_builder_config,
            "modules.process.zoning_filter": _zoning_filter_config,
            "modules.output.rate_limiter": _rate_limiter_config,
            "modules.output.telegram_notifier": _telegram_notifier_config,
            "modules.status.prometheus_exporter": _prometheus_exporter_config,
            "modules.dashboard.control_api": _control_api_config,
            "modules.dashboard.telegram_bot": _telegram_control_bot_config,
            "modules.storage.retention": _storage_retention_config,
            "modules.analytics.event_logger": _analytics_logger_config,
        }

        try:
            builder = builders[module_name]
        except KeyError as exc:
            raise KeyError(f"No module configuration defined for {module_name}") from exc
        return builder()


class ConfigService:
    """
    Runtime facade for loading, validating, and distributing configuration.
    """

    def __init__(
        self,
        *,
        config_dir: str | Path | None = None,
        settings: Dynaconf | None = None,
    ) -> None:
        self._config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        settings_files = [self._config_dir / name for name in CONFIG_FILENAMES]
        existing_files = [str(path) for path in settings_files if path.exists()]
        if not existing_files:
            raise ConfigError(
                f"No configuration files found in {self._config_dir}. "
                "Expected at least config.yaml."
            )

        self._settings = settings or Dynaconf(
            envvar_prefix="SPYONCINO",
            settings_files=existing_files,
            load_dotenv=True,
            environments=False,
        )
        self._snapshot = self._build_snapshot()
        self._snapshot.storage.ensure_directories()

    @property
    def snapshot(self) -> ConfigSnapshot:
        """Latest validated configuration snapshot."""
        return self._snapshot

    def refresh(self) -> ConfigSnapshot:
        """Reload configuration files and rebuild the snapshot."""
        self._settings.reload()
        self._snapshot = self._build_snapshot()
        self._snapshot.storage.ensure_directories()
        return self._snapshot

    def apply_changes(self, changes: dict[str, Any]) -> ConfigSnapshot:
        """
        Merge the provided changes into the current configuration snapshot.

        This does not persist the changes to disk but enables hot reload flows.
        """
        raw = self._settings.as_dict()
        merged = _deep_merge(raw, changes)
        self._snapshot = self._build_snapshot(merged)
        self._snapshot.storage.ensure_directories()
        return self._snapshot

    def module_config_for(self, module: str | type[BaseModule] | BaseModule) -> ModuleConfig:
        """
        Convenient wrapper around ConfigSnapshot.module_config that accepts
        module names, classes, or instances.
        """
        if isinstance(module, BaseModule):
            module_name = module.name
        elif isinstance(module, str):
            module_name = module
        else:
            module_name = getattr(module, "name", module.__name__)
        return self._snapshot.module_config(module_name)

    def _build_snapshot(self, raw: dict[str, Any] | None = None) -> ConfigSnapshot:
        data = self._extract_snapshot_data(raw or self._settings.as_dict())
        try:
            return ConfigSnapshot.model_validate(data)
        except ValidationError as exc:
            raise ConfigError("Configuration validation failed") from exc

    def _extract_snapshot_data(self, raw: dict[str, Any]) -> dict[str, Any]:
        data = {
            "camera": _section(raw, "camera"),
            "detection": _section(raw, "detection"),
            "alert_pipeline": _section(raw, "alert_pipeline"),
            "dedupe": _section(raw, "dedupe"),
            "storage": _section(raw, "storage"),
            "notifications": _section(raw, "notifications"),
            "rate_limit": _section(raw, "rate_limit"),
            "zoning": _section(raw, "zoning"),
            "clip": _section(raw, "clip"),
            "advanced": _section(raw, "advanced"),
            "analytics": _section(raw, "analytics"),
            "control_api": _section(raw, "control_api"),
            "telegram": _section(raw, "telegram"),
            "telegram_security": _section(raw, "security"),
            "telegram_behavior": _section(raw, "behavior"),
            "telegram_rate_limiting": _section(raw, "rate_limiting"),
            "authentication": _section(raw, "authentication"),
        }
        return data


__all__ = [
    "AdvancedSettings",
    "AlertPipelineSettings",
    "AnalyticsSettings",
    "CameraSettings",
    "ClipSettings",
    "ConfigError",
    "ConfigService",
    "ConfigSnapshot",
    "ControlApiSettings",
    "DetectionSettings",
    "StorageSettings",
    "ZoneDefinition",
    "ZoningSettings",
]
