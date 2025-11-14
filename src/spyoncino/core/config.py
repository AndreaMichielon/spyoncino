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
from typing import Any

from dynaconf import Dynaconf
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .contracts import BaseModule, ModuleConfig


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """Case-insensitive dictionary lookup helper."""
    value = raw.get(key) or raw.get(key.upper()) or raw.get(key.lower())
    if isinstance(value, dict):
        return value
    return {}


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
    snapshot_subdir: str = Field(default="snapshots")
    gif_subdir: str = Field(default="gifs")

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path(cls, value: Any) -> Path:
        if isinstance(value, Path):
            return value
        return Path(value)

    @property
    def snapshot_dir(self) -> Path:
        return self.path / self.snapshot_subdir

    @property
    def gif_dir(self) -> Path:
        return self.path / self.gif_subdir

    def ensure_directories(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.gif_dir.mkdir(parents=True, exist_ok=True)


class NotificationSettings(BaseModel):
    """Notification preferences (GIFs, etc.)."""

    model_config = ConfigDict(extra="ignore")

    gif_for_motion: bool = Field(default=False)
    gif_for_person: bool = Field(default=True)
    gif_duration: float = Field(default=3.0)
    gif_fps: int = Field(default=10)


class RateLimitSettings(BaseModel):
    """Throttle outgoing notifications."""

    model_config = ConfigDict(extra="ignore")

    input_topic: str = Field(default="event.snapshot.ready")
    output_topic: str = Field(default="event.snapshot.allowed")
    max_events: int = Field(default=5)
    per_seconds: float = Field(default=60.0)
    key_field: str = Field(default="camera_id")


class AdvancedSettings(BaseModel):
    """Advanced tuning knobs used by multiple modules."""

    model_config = ConfigDict(extra="ignore")

    telegram_read_timeout: float = Field(default=30.0)
    telegram_write_timeout: float = Field(default=60.0)


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


class ConfigSnapshot(BaseModel):
    """
    Validated, strongly typed view of the merged configuration.

    Provides helpers to derive per-module configuration dictionaries.
    """

    model_config = ConfigDict(extra="ignore")

    camera: CameraSettings = Field(default_factory=CameraSettings)
    detection: DetectionSettings = Field(default_factory=DetectionSettings)
    dedupe: DedupeSettings = Field(default_factory=DedupeSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    advanced: AdvancedSettings = Field(default_factory=AdvancedSettings)
    telegram: TelegramSecrets = Field(default_factory=TelegramSecrets)
    telegram_security: TelegramSecuritySettings = Field(default_factory=TelegramSecuritySettings)
    telegram_behavior: TelegramBehaviorSettings = Field(default_factory=TelegramBehaviorSettings)
    telegram_rate_limiting: TelegramRateLimitSettings = Field(
        default_factory=TelegramRateLimitSettings
    )

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
            return ModuleConfig(options=options)

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
            return ModuleConfig(
                options={
                    "frame_topics": [self.camera.topic],
                    "detection_topic": self.dedupe.output_topic,
                    "output_dir": str(self.storage.gif_dir),
                    "fps": self.notifications.gif_fps,
                    "duration_seconds": self.notifications.gif_duration,
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

        builders: dict[str, Callable[[], ModuleConfig]] = {
            "modules.input.camera_simulator": _camera_sim_config,
            "modules.input.rtsp_camera": _rtsp_camera_config,
            "modules.process.motion_detector": _motion_detector_config,
            "modules.process.yolo_detector": _yolo_detector_config,
            "modules.event.deduplicator": _deduplicator_config,
            "modules.event.snapshot_writer": _snapshot_writer_config,
            "modules.event.gif_builder": _gif_builder_config,
            "modules.output.rate_limiter": _rate_limiter_config,
            "modules.output.telegram_notifier": _telegram_notifier_config,
            "modules.status.prometheus_exporter": _prometheus_exporter_config,
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

    def _build_snapshot(self) -> ConfigSnapshot:
        raw = self._settings.as_dict()
        data = {
            "camera": _section(raw, "camera"),
            "detection": _section(raw, "detection"),
            "dedupe": _section(raw, "dedupe"),
            "storage": _section(raw, "storage"),
            "notifications": _section(raw, "notifications"),
            "rate_limit": _section(raw, "rate_limit"),
            "advanced": _section(raw, "advanced"),
            "telegram": _section(raw, "telegram"),
            "telegram_security": _section(raw, "security"),
            "telegram_behavior": _section(raw, "behavior"),
            "telegram_rate_limiting": _section(raw, "rate_limiting"),
        }
        try:
            return ConfigSnapshot.model_validate(data)
        except ValidationError as exc:
            raise ConfigError("Configuration validation failed") from exc


__all__ = [
    "AdvancedSettings",
    "CameraSettings",
    "ConfigError",
    "ConfigService",
    "ConfigSnapshot",
    "StorageSettings",
]
