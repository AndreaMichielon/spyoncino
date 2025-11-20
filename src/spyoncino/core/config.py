"""
Dynaconf-powered configuration loader with Pydantic validation.

The configuration service is responsible for loading the layered YAML
configuration files described in `TODO_ARCHITECTURE.md`, validating them, and
producing module-friendly `ModuleConfig` instances so the orchestrator can wire
modules without hand-written dictionaries.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

from dynaconf import Dynaconf
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .contracts import BaseModule, ModuleConfig


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """Case-insensitive dictionary lookup helper."""
    value = raw.get(key) or raw.get(key.upper()) or raw.get(key.lower())
    if isinstance(value, dict):
        return value
    return {}


def _section_list(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """List-aware helper for case-insensitive lookups."""
    value = raw.get(key) or raw.get(key.upper()) or raw.get(key.lower())
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


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

    DEFAULT_WIDTH: ClassVar[int] = 1280
    DEFAULT_HEIGHT: ClassVar[int] = 720
    DEFAULT_FPS: ClassVar[int] = 15

    camera_id: str = Field(default="default")
    usb_port: int | str = Field(default=0)
    rtsp_url: str | None = Field(default=None)
    interval_seconds: float = Field(default=1.0)
    width: int | None = Field(default=DEFAULT_WIDTH)
    height: int | None = Field(default=DEFAULT_HEIGHT)
    fps: int | None = Field(default=DEFAULT_FPS)
    notes: str | None = Field(default=None)

    @property
    def topic(self) -> str:
        return f"camera.{self.camera_id}.frame"

    def resolved_dimensions(self) -> tuple[int, int]:
        """
        Return width/height with sensible fallbacks for modules that require explicit numbers
        (e.g., camera simulator).
        """

        width = self.width if self.width is not None else self.DEFAULT_WIDTH
        height = self.height if self.height is not None else self.DEFAULT_HEIGHT
        return width, height


class DetectionSettings(BaseModel):
    """Subset of detection options required for Week 2 features."""

    model_config = ConfigDict(extra="ignore")

    interval: float = Field(default=2.0)
    motion_threshold: int = Field(default=5)
    confidence: float = Field(default=0.25)
    model_path: str | None = Field(default=None)
    class_filter: list[str] = Field(default_factory=list)
    label_cooldown_seconds: float = Field(
        default=30.0,
        validation_alias=AliasChoices("label_cooldown_seconds", "person_cooldown_seconds"),
        serialization_alias="label_cooldown_seconds",
    )
    bbox_overlap_threshold: float = Field(default=0.6)
    alert_labels: list[str] = Field(default_factory=lambda: ["person"])

    @property
    def person_cooldown_seconds(self) -> float:
        """Backward-compatible alias for legacy config code."""
        return self.label_cooldown_seconds


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
    snap_subdir: str = Field(default="snapshots")
    gif_subdir: str = Field(default="gifs")
    video_subdir: str = Field(default="clips")
    s3: S3SyncSettings = Field(default_factory=lambda: S3SyncSettings())

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
        return self.path / self.snap_subdir

    @property
    def gif_dir(self) -> Path:
        return self.path / self.gif_subdir

    @property
    def clip_dir(self) -> Path:
        return self.path / self.video_subdir

    def ensure_directories(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.gif_dir.mkdir(parents=True, exist_ok=True)
        self.clip_dir.mkdir(parents=True, exist_ok=True)


class S3SyncSettings(BaseModel):
    """Remote storage replication settings."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=False)
    bucket: str | None = Field(default=None)
    region_name: str | None = Field(default=None)
    prefix: str = Field(default="")
    upload_topics: list[str] = Field(
        default_factory=lambda: [
            "event.snapshot.ready",
            "event.snapshot.allowed",
            "event.gif.ready",
            "event.clip.ready",
        ]
    )
    lifecycle_tags: dict[str, str] = Field(default_factory=dict)
    endpoint_url: str | None = Field(default=None)
    aws_access_key_id: str | None = Field(default=None)
    aws_secret_access_key: str | None = Field(default=None)
    aws_session_token: str | None = Field(default=None)
    max_concurrency: int = Field(default=2, ge=1)
    multipart_threshold_mb: float = Field(default=8.0, ge=0.5)
    multipart_chunks_mb: float = Field(default=8.0, ge=0.5)
    publish_topic: str = Field(default="storage.s3.synced")
    discrepancy_topic: str = Field(default="storage.discrepancy")
    remote_tracking_window_seconds: float = Field(default=3600.0, ge=60.0)


class SnapshotOutputSettings(BaseModel):
    """Snapshot artifact overrides."""

    model_config = ConfigDict(extra="ignore")

    max_dimension: int | None = Field(default=None, description="Max width/height in pixels")


class GifOutputSettings(BaseModel):
    """GIF artifact preferences."""

    model_config = ConfigDict(extra="ignore")

    duration_seconds: float = Field(default=3.0)
    fps: int = Field(default=15)
    max_file_size_mb: float = Field(default=50.0)
    max_dimension: int = Field(
        default=640, description="Maximum width or height in pixels for GIF frames"
    )


class VideoNotificationSettings(BaseModel):
    """Video notification constraints."""

    model_config = ConfigDict(extra="ignore")

    duration_seconds: float = Field(default=5.0)
    fps: int = Field(default=12)
    max_file_size_mb: float = Field(default=50.0)
    max_dimension: int | None = Field(default=None, description="Max width/height in pixels")


class NotificationSettings(BaseModel):
    """Notification preferences (snap/gif/video routing)."""

    model_config = ConfigDict(extra="ignore")

    output_for_motion: Literal["text", "snap", "gif", "video", "none", None] = Field(default="text")
    output_for_detection: Literal["text", "snap", "gif", "video", "none", None] = Field(
        default="gif"
    )
    snap: SnapshotOutputSettings = Field(default_factory=SnapshotOutputSettings)
    gif: GifOutputSettings = Field(default_factory=GifOutputSettings)
    video: VideoNotificationSettings = Field(default_factory=VideoNotificationSettings)
    # Anti-spam cooldown settings for notifications
    cooldown_enabled: bool = Field(
        default=True, description="Enable cooldown filtering for notifications"
    )
    cooldown_seconds: float = Field(
        default=30.0, description="Minimum seconds between notifications per camera/type"
    )
    bbox_iou_threshold: float = Field(
        default=0.6, description="IoU threshold for bbox overlap detection"
    )
    timeout_seconds: float = Field(
        default=5.0, description="Reset cooldown after this many seconds of inactivity"
    )

    def wants_motion_gif(self) -> bool:
        return (self.output_for_motion or "").lower() == "gif"

    def wants_detection_gif(self) -> bool:
        return (self.output_for_detection or "").lower() == "gif"

    def wants_motion_video(self) -> bool:
        return (self.output_for_motion or "").lower() == "video"

    def wants_detection_video(self) -> bool:
        return (self.output_for_detection or "").lower() == "video"


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
    zones: list[ZoneDefinition] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_zones(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = data.get("zones")
        data["zones"] = cls._flatten_zone_definitions(raw)
        return data

    @staticmethod
    def _flatten_zone_definitions(raw: Any) -> list[dict[str, Any]]:
        """
        Accepts multiple shorthand formats for zones and returns a list of plain zone dicts.
        Supported shorthands:
          1. List of {camera_id, zones: {zone_id: {...}}}
          2. Dict camera_id -> {zone_id: {...}}
          3. Values where zone body is just a bounds list [x1, y1, x2, y2]
        """

        def _normalize_bounds(value: Any) -> tuple[float, float, float, float] | None:
            if isinstance(value, Sequence) and len(value) == 4:
                try:
                    return tuple(float(v) for v in value)  # type: ignore[return-value]
                except (TypeError, ValueError):
                    return None
            return None

        def _expand_zone_map(camera_id: str, zone_map: Any) -> list[dict[str, Any]]:
            if not isinstance(zone_map, dict):
                return []
            result: list[dict[str, Any]] = []
            for zone_id, zone_data in zone_map.items():
                if zone_data is None:
                    continue
                entry: dict[str, Any] = {
                    "camera_id": camera_id,
                    "zone_id": str(zone_id),
                }
                bounds = _normalize_bounds(zone_data)
                if bounds is not None:
                    entry["bounds"] = bounds
                elif isinstance(zone_data, dict):
                    entry.update(zone_data)
                else:
                    continue
                result.append(entry)
            return result

        if raw is None:
            return []
        flattened: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            for camera_id, zone_map in raw.items():
                flattened.extend(_expand_zone_map(str(camera_id), zone_map))
            return flattened
        if isinstance(raw, list):
            for entry in raw:
                if (
                    isinstance(entry, dict)
                    and "zones" in entry
                    and not isinstance(entry.get("zone_id"), str)
                ):
                    camera_id = str(entry.get("camera_id") or "default")
                    flattened.extend(_expand_zone_map(camera_id, entry.get("zones")))
                elif isinstance(entry, dict):
                    flattened.append(entry)
            return flattened
        return []


class ClipSettings(BaseModel):
    """Configuration for MP4 clip generation."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=False)
    detection_topic: str = Field(default="process.motion.unique")
    output_topic: str = Field(default="event.clip.ready")
    duration_seconds: float = Field(default=5.0)
    fps: int = Field(default=12)
    max_artifacts: int | None = Field(default=25)


class TlsSettings(BaseModel):
    """Reusable TLS configuration for HTTP surfaces."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=False)
    certfile: str | None = Field(default=None)
    keyfile: str | None = Field(default=None)
    ca_certfile: str | None = Field(default=None)
    require_client_cert: bool = Field(default=False)


class ControlApiSettings(BaseModel):
    """Control API (FastAPI) configuration."""

    model_config = ConfigDict(extra="ignore")

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8080)
    serve_api: bool = Field(default=True)
    command_topic: str = Field(default="dashboard.control.command")
    config_topic: str = Field(default="config.update")
    tls: TlsSettings = Field(default_factory=TlsSettings)


class WebsocketGatewaySettings(BaseModel):
    """Realtime dashboard streaming surface configuration."""

    model_config = ConfigDict(extra="ignore")

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8081)
    serve_http: bool = Field(default=True)
    tls: TlsSettings = Field(default_factory=TlsSettings)
    topics: list[str] = Field(
        default_factory=lambda: [
            "status.health.summary",
            "status.bus",
            "notify.telegram.sent",
            "analytics.persistence.cursor",
            "storage.stats",
        ]
    )
    buffer_size: int = Field(default=256, ge=10)
    idle_timeout_seconds: float = Field(default=30.0, ge=1.0)


class ResilienceScenarioSettings(BaseModel):
    """Single chaos scenario description."""

    model_config = ConfigDict(extra="ignore")

    name: str
    topic: str
    latency_ms: float = Field(default=0.0, ge=0.0)
    drop_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    enabled: bool = Field(default=True)


class ResilienceSettings(BaseModel):
    """Chaos tooling configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=False)
    scenarios: list[ResilienceScenarioSettings] = Field(default_factory=list)
    command_topic: str = Field(default="dashboard.control.command")
    status_topic: str = Field(default="status.resilience.event")


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
    database_url: str | None = Field(
        default=None, description="Optional SQLModel-compatible database URL."
    )
    detection_topics: list[str] = Field(default_factory=lambda: ["process.motion.unique"])
    alert_topics: list[str] = Field(default_factory=lambda: ["process.alert.detected"])
    storage_topic: str = Field(default="storage.stats")
    cursor_topic: str = Field(default="analytics.persistence.cursor")
    topics: list[str] = Field(
        default_factory=lambda: [
            "process.motion.unique",
            "process.alert.detected",
            "storage.stats",
        ]
    )
    backfill_history_seconds: int = Field(default=0, ge=0)
    legacy_logger_enabled: bool = Field(
        default=True,
        description="Whether to continue emitting legacy SQLite analytics alongside DB logger.",
    )


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


class ModuleManifestEntry(BaseModel):
    """Declarative module entry derived from manifest lists (outputs/dashboards/etc.)."""

    model_config = ConfigDict(extra="allow")

    module: str = Field(
        description="Fully qualified module name, e.g. modules.output.telegram_notifier."
    )
    enabled: bool = Field(default=True)
    additive: bool = Field(
        default=False,
        description="When true, manifests add extra instances and never override defaults.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Optional list of module names this entry depends on (startup order).",
    )
    phase: str | None = Field(
        default=None,
        description="Optional coarse grouping hint (e.g., inputs/process/events/outputs).",
    )
    camera_id: str | None = Field(
        default=None, description="Optional camera binding for per-camera module instances."
    )
    options: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    def to_module_config(self) -> ModuleConfig:
        """Translate manifest entries into ModuleConfig objects consumed by modules."""

        options = dict(self.options)
        # Pass-through dependencies to ModuleConfig options so the orchestrator can read them
        if self.depends_on and "depends_on" not in options:
            options["depends_on"] = list(self.depends_on)
        if self.camera_id and "camera_id" not in options:
            options["camera_id"] = self.camera_id
        return ModuleConfig(enabled=self.enabled, options=options)


class ConfigSnapshot(BaseModel):
    """
    Validated, strongly typed view of the merged configuration.

    Provides helpers to derive per-module configuration dictionaries.
    """

    model_config = ConfigDict(extra="ignore")

    camera: CameraSettings = Field(default_factory=CameraSettings)
    cameras: list[CameraSettings] = Field(default_factory=list)
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
    websocket_gateway: WebsocketGatewaySettings = Field(default_factory=WebsocketGatewaySettings)
    telegram: TelegramSecrets = Field(default_factory=TelegramSecrets)
    telegram_security: TelegramSecuritySettings = Field(default_factory=TelegramSecuritySettings)
    telegram_behavior: TelegramBehaviorSettings = Field(default_factory=TelegramBehaviorSettings)
    telegram_rate_limiting: TelegramRateLimitSettings = Field(
        default_factory=TelegramRateLimitSettings
    )
    authentication: AuthenticationSettings = Field(default_factory=AuthenticationSettings)
    resilience: ResilienceSettings = Field(default_factory=ResilienceSettings)
    outputs: list[ModuleManifestEntry] = Field(default_factory=list)
    dashboards: list[ModuleManifestEntry] = Field(default_factory=list)
    modules: list[ModuleManifestEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ensure_cameras(self) -> ConfigSnapshot:
        cameras = list(self.cameras)
        if not cameras:
            cameras = [self.camera]
        self.cameras = cameras
        self.camera = cameras[0]
        return self

    @property
    def primary_camera(self) -> CameraSettings:
        return self.camera

    def camera_by_id(self, camera_id: str) -> CameraSettings:
        for camera in self.cameras:
            if camera.camera_id == camera_id:
                return camera
        raise KeyError(f"No camera configured with id '{camera_id}'")

    def camera_topics(self) -> list[str]:
        return [camera.topic for camera in self.cameras]

    def module_config(self, module_name: str, *, camera_id: str | None = None) -> ModuleConfig:
        configs = self.module_configs(module_name, camera_id=camera_id)
        if not configs:
            raise KeyError(f"No module configuration defined for {module_name}")
        return configs[0]

    def module_configs(
        self, module_name: str, *, camera_id: str | None = None
    ) -> list[ModuleConfig]:
        """Produce a ModuleConfig tailored for the requested module."""

        manifest_configs = [
            self._apply_manifest_defaults(module_name, entry.to_module_config())
            for entry in self._manifest_entries(module_name)
            if entry.enabled
        ]
        if manifest_configs:
            return manifest_configs

        def _selected_cameras() -> list[CameraSettings]:
            if camera_id is None:
                return list(self.cameras)
            return [self.camera_by_id(camera_id)]

        def _camera_sim_config() -> list[ModuleConfig]:
            configs: list[ModuleConfig] = []
            for camera in _selected_cameras():
                frame_width, frame_height = camera.resolved_dimensions()
                configs.append(
                    ModuleConfig(
                        options={
                            "camera_id": camera.camera_id,
                            "interval_seconds": camera.interval_seconds,
                            "frame_width": frame_width,
                            "frame_height": frame_height,
                        }
                    )
                )
            return configs

        def _usb_camera_config() -> list[ModuleConfig]:
            configs: list[ModuleConfig] = []
            for camera in _selected_cameras():
                port = camera.usb_port
                options: dict[str, Any] = {
                    "camera_id": camera.camera_id,
                    "fps": camera.fps,
                    "frame_width": camera.width,
                    "frame_height": camera.height,
                }
                if isinstance(port, int):
                    options["device_index"] = port
                elif port is not None:
                    options["device_path"] = str(port)
                else:
                    continue
                configs.append(ModuleConfig(options=options))
            return configs

        def _rtsp_camera_config() -> list[ModuleConfig]:
            configs: list[ModuleConfig] = []
            for camera in _selected_cameras():
                if not camera.rtsp_url:
                    continue
                configs.append(
                    ModuleConfig(
                        options={
                            "camera_id": camera.camera_id,
                            "rtsp_url": camera.rtsp_url,
                            "fps": camera.fps,
                        }
                    )
                )
            return configs

        def _motion_detector_config() -> ModuleConfig:
            return ModuleConfig(
                options={
                    "input_topics": self.camera_topics(),
                    "input_topic": self.primary_camera.topic,
                    "interval_seconds": self.detection.interval,
                    "motion_threshold": self.detection.motion_threshold,
                }
            )

        def _yolo_detector_config() -> ModuleConfig:
            options: dict[str, Any] = {
                "input_topics": self.camera_topics(),
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
                    "frame_topics": self.camera_topics(),
                    "detection_topic": self.dedupe.output_topic,
                    "output_dir": str(self.storage.snapshot_dir),
                    "max_dimension": self.notifications.snap.max_dimension,
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
            gif_settings = self.notifications.gif
            detection_topic = (
                self.alert_pipeline.output_topic
                if self.notifications.wants_detection_gif()
                else self.dedupe.output_topic
            )
            enabled = (
                self.notifications.wants_detection_gif() or self.notifications.wants_motion_gif()
            )
            return ModuleConfig(
                enabled=enabled,
                options={
                    "frame_topics": self.camera_topics(),
                    "detection_topic": detection_topic,
                    "output_dir": str(self.storage.gif_dir),
                    "fps": gif_settings.fps,
                    "duration_seconds": gif_settings.duration_seconds,
                    "max_dimension": gif_settings.max_dimension,
                },
            )

        def _clip_builder_config() -> ModuleConfig:
            # Wire clip builder based on notification preferences, similar to GIF builder
            # If we want video for detections, listen to alert pipeline; if for motion, listen to dedupe
            # Prioritize detection if both are wanted (consistent with GIF builder behavior)
            detection_topic = (
                self.alert_pipeline.output_topic
                if self.notifications.wants_detection_video()
                else (
                    self.dedupe.output_topic
                    if self.notifications.wants_motion_video()
                    else self.clip.detection_topic
                )
            )
            # Only enable if we actually want video for either motion or detection
            enabled = self.clip.enabled and (
                self.notifications.wants_motion_video()
                or self.notifications.wants_detection_video()
            )
            return ModuleConfig(
                enabled=enabled,
                options={
                    "enabled": enabled,  # ClipBuilder reads from options
                    "frame_topics": self.camera_topics(),
                    "detection_topic": detection_topic,
                    "output_topic": self.clip.output_topic,
                    "output_dir": str(self.storage.clip_dir),
                    "fps": self.clip.fps,
                    "duration_seconds": self.clip.duration_seconds,
                    "max_artifacts": self.clip.max_artifacts,
                    "max_dimension": self.notifications.video.max_dimension,
                },
            )

        def _zoning_filter_config() -> ModuleConfig:
            camera_dimensions = {
                camera.camera_id: {
                    "width": camera.resolved_dimensions()[0],
                    "height": camera.resolved_dimensions()[1],
                }
                for camera in self.cameras
            }
            return ModuleConfig(
                options={
                    "enabled": self.zoning.enabled,
                    "input_topic": self.zoning.input_topic,
                    "output_topic": self.zoning.output_topic,
                    "unmatched_topic": self.zoning.unmatched_topic,
                    "camera_dimensions": camera_dimensions,
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
                    "tls_enabled": self.control_api.tls.enabled,
                    "tls_certfile": self.control_api.tls.certfile,
                    "tls_keyfile": self.control_api.tls.keyfile,
                    "tls_ca_certfile": self.control_api.tls.ca_certfile,
                    "tls_require_client_cert": self.control_api.tls.require_client_cert,
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
            chat_id = self._default_chat_id()
            chat_targets = self._default_chat_targets()
            # Determine topics based on notification preferences
            motion_pref = (self.notifications.output_for_motion or "text").strip().lower()
            detection_pref = (self.notifications.output_for_detection or "gif").strip().lower()

            # Snapshots come from motion events, so only enable if motion wants them
            wants_motion_snapshot = motion_pref in ("text", "snap")
            wants_motion_text = motion_pref == "text"
            wants_detection_text = detection_pref == "text"
            wants_motion_gif = motion_pref == "gif"
            wants_motion_video = motion_pref == "video"
            wants_detection_gif = detection_pref == "gif"
            wants_detection_video = detection_pref == "video"

            snapshot_topic = self.rate_limit.output_topic if wants_motion_snapshot else None
            gif_topic = "event.gif.ready" if (wants_motion_gif or wants_detection_gif) else None
            clip_topic = (
                self.clip.output_topic
                if (self.clip.enabled and (wants_motion_video or wants_detection_video))
                else None
            )
            snapshot_delivery = "text" if (wants_motion_text or wants_detection_text) else "photo"

            return ModuleConfig(
                options={
                    "token": self.telegram.token,
                    "chat_id": chat_id,
                    "chat_targets": chat_targets,
                    "topic": snapshot_topic,
                    "gif_topic": gif_topic,
                    "clip_topic": clip_topic,
                    "snapshot_delivery": snapshot_delivery,
                    "read_timeout": self.advanced.telegram_read_timeout,
                    "write_timeout": self.advanced.telegram_write_timeout,
                    "send_typing_action": self.telegram_behavior.send_typing_action,
                    "gif_notification_fps": self.notifications.gif.fps,
                    "cooldown_enabled": self.notifications.cooldown_enabled,
                    "cooldown_seconds": self.notifications.cooldown_seconds,
                    "bbox_iou_threshold": self.notifications.bbox_iou_threshold,
                    "timeout_seconds": self.notifications.timeout_seconds,
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
                    "remote_tracking": self.storage.s3.enabled,
                    "remote_topic": self.storage.s3.publish_topic,
                    "discrepancy_topic": self.storage.s3.discrepancy_topic,
                    "remote_window_seconds": self.storage.s3.remote_tracking_window_seconds,
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

        def _analytics_db_logger_config() -> ModuleConfig:
            db_url = (
                self.analytics.database_url
                or f"sqlite:///{self.storage.path / self.analytics.db_filename}"
            )
            dedup_topics = [
                *self.analytics.detection_topics,
                *self.analytics.alert_topics,
                self.analytics.storage_topic,
            ]
            topics = self.analytics.topics or list(dict.fromkeys(dedup_topics))
            return ModuleConfig(
                options={
                    "database_url": db_url,
                    "topics": topics,
                    "cursor_topic": self.analytics.cursor_topic,
                    "backfill_history_seconds": self.analytics.backfill_history_seconds,
                }
            )

        def _s3_uploader_config() -> ModuleConfig:
            settings = self.storage.s3
            return ModuleConfig(
                options={
                    "enabled": settings.enabled,
                    "root_dir": str(self.storage.path),
                    "bucket": settings.bucket,
                    "region_name": settings.region_name,
                    "prefix": settings.prefix,
                    "upload_topics": settings.upload_topics,
                    "lifecycle_tags": settings.lifecycle_tags,
                    "endpoint_url": settings.endpoint_url,
                    "aws_access_key_id": settings.aws_access_key_id,
                    "aws_secret_access_key": settings.aws_secret_access_key,
                    "aws_session_token": settings.aws_session_token,
                    "max_concurrency": settings.max_concurrency,
                    "multipart_threshold_mb": settings.multipart_threshold_mb,
                    "multipart_chunks_mb": settings.multipart_chunks_mb,
                    "publish_topic": settings.publish_topic,
                    "discrepancy_topic": settings.discrepancy_topic,
                    "queue_size": 64,
                }
            )

        def _websocket_gateway_config() -> ModuleConfig:
            ws = self.websocket_gateway
            return ModuleConfig(
                options={
                    "host": ws.host,
                    "port": ws.port,
                    "serve_http": ws.serve_http,
                    "tls_enabled": ws.tls.enabled,
                    "tls_certfile": ws.tls.certfile,
                    "tls_keyfile": ws.tls.keyfile,
                    "tls_ca_certfile": ws.tls.ca_certfile,
                    "tls_require_client_cert": ws.tls.require_client_cert,
                    "topics": ws.topics,
                    "buffer_size": ws.buffer_size,
                    "idle_timeout_seconds": ws.idle_timeout_seconds,
                }
            )

        def _resilience_tester_config() -> ModuleConfig:
            res = self.resilience
            return ModuleConfig(
                options={
                    "enabled": res.enabled,
                    "scenarios": [scenario.model_dump(mode="python") for scenario in res.scenarios],
                    "command_topic": res.command_topic,
                    "status_topic": res.status_topic,
                }
            )

        def _command_handler_config() -> ModuleConfig:
            """Default config for DashboardCommandHandler."""
            return ModuleConfig(
                options={
                    "command_topic": "dashboard.control.command",
                    "snapshot_result_topic": "dashboard.snapshot.result",
                    "timeline_result_topic": "dashboard.timeline.result",
                    "analytics_result_topic": "dashboard.analytics.result",
                    "events_db_path": str(self.storage.path / "events.db"),
                }
            )

        def _recordings_service_config() -> ModuleConfig:
            """Default config for RecordingsService."""
            # Use gifs directory for recordings (where GIFs are stored in modular system)
            return ModuleConfig(
                options={
                    "command_topic": "dashboard.control.command",
                    "list_result_topic": "dashboard.recordings.list.result",
                    "get_result_topic": "dashboard.recordings.get.result",
                    "events_root": str(self.storage.gif_dir),
                    "default_limit": 20,
                }
            )

        builders: dict[str, Callable[[], list[ModuleConfig]]] = {
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
            "modules.status.resilience_tester": _resilience_tester_config,
            "modules.dashboard.control_api": _control_api_config,
            "modules.dashboard.command_handler": _command_handler_config,
            "modules.dashboard.recordings_service": _recordings_service_config,
            "modules.dashboard.telegram_bot": _telegram_control_bot_config,
            "modules.dashboard.websocket_gateway": _websocket_gateway_config,
            "modules.storage.retention": _storage_retention_config,
            "modules.storage.s3_uploader": _s3_uploader_config,
            "modules.analytics.event_logger": _analytics_logger_config,
            "modules.analytics.db_logger": _analytics_db_logger_config,
        }

        try:
            builder = builders[module_name]
        except KeyError as exc:
            raise KeyError(f"No module configuration defined for {module_name}") from exc
        result = builder()
        if isinstance(result, list):
            return result
        return [result]

    def _default_chat_targets(self) -> list[int | str]:
        """Resolve all Telegram destinations inferred from config."""

        targets: list[int | str] = []

        def _append_candidate(value: int | str | None) -> None:
            if value is None:
                return
            if value not in targets:
                targets.append(value)

        _append_candidate(self.telegram.chat_id)
        _append_candidate(self.telegram_security.notification_chat_id)
        _append_candidate(self.authentication.superuser_id)
        for user_id in self.authentication.user_whitelist:
            _append_candidate(user_id)
        return targets

    def _default_chat_id(self) -> int | str | None:
        """Return the primary chat target for backwards compatibility."""

        targets = self._default_chat_targets()
        if targets:
            return targets[0]
        return None

    def _manifest_entries(self, module_name: str) -> list[ModuleManifestEntry]:
        """Return additive-only manifest entries for the requested module name.

        Manifests are treated as additive-only: they can add extra instances of modules,
        but never replace the default builder-derived configuration. To opt-in, entries
        must set `additive: true`.
        """

        matches: list[ModuleManifestEntry] = []
        for collection in (self.outputs, self.dashboards, self.modules):
            for entry in collection:
                if entry.module != module_name:
                    continue
                if not entry.additive:
                    # Ignore non-additive entries to keep default config as the primary source
                    continue
                matches.append(entry)
        return matches

    def _apply_manifest_defaults(self, module_name: str, config: ModuleConfig) -> ModuleConfig:
        """Populate manifest-derived ModuleConfig with sane defaults."""

        options = dict(config.options)

        def _needs_resolution(value: Any) -> bool:
            return isinstance(value, str) and value.startswith("@secrets")

        if module_name == "modules.output.telegram_notifier":
            if "token" not in options or _needs_resolution(options["token"]):
                options["token"] = self.telegram.token
            if options.get("token") is None:
                raise ConfigError("Telegram notifier manifest entries require a bot token.")
            chat_id = options.get("chat_id")
            default_chat = self._default_chat_id()
            default_targets = self._default_chat_targets()
            if chat_id is None or _needs_resolution(chat_id):
                options["chat_id"] = default_chat
            chat_targets = options.get("chat_targets")
            if chat_targets is None or _needs_resolution(chat_targets):
                options["chat_targets"] = default_targets
            if options.get("chat_id") is None:
                raise ConfigError("Telegram notifier manifest entries require a chat_id.")
            motion_pref = (self.notifications.output_for_motion or "text").strip().lower()
            detection_pref = (self.notifications.output_for_detection or "gif").strip().lower()

            # Snapshots come from motion events, so only enable if motion wants them
            wants_motion_snapshot = motion_pref in ("text", "snap")
            wants_motion_text = motion_pref == "text"
            wants_detection_text = detection_pref == "text"
            wants_motion_gif = motion_pref == "gif"
            wants_motion_video = motion_pref == "video"
            wants_detection_gif = detection_pref == "gif"
            wants_detection_video = detection_pref == "video"

            # Always override topics based on preferences (don't check if already set)
            snapshot_topic_default = self.rate_limit.output_topic if wants_motion_snapshot else None
            gif_topic_default = (
                "event.gif.ready" if (wants_motion_gif or wants_detection_gif) else None
            )
            clip_topic_default = (
                self.clip.output_topic
                if (self.clip.enabled and (wants_motion_video or wants_detection_video))
                else None
            )

            options["topic"] = snapshot_topic_default
            options["gif_topic"] = gif_topic_default
            options["clip_topic"] = clip_topic_default

            # Set delivery mode based on preferences
            if "snapshot_delivery" not in options:
                # Use text delivery if either motion or detection wants text
                default_delivery = (
                    "text" if (wants_motion_text or wants_detection_text) else "photo"
                )
                options["snapshot_delivery"] = default_delivery
            options.setdefault("read_timeout", self.advanced.telegram_read_timeout)
            options.setdefault("write_timeout", self.advanced.telegram_write_timeout)
            options.setdefault("send_typing_action", self.telegram_behavior.send_typing_action)
            options.setdefault("gif_notification_fps", self.notifications.gif.fps)
            options.setdefault("cooldown_enabled", self.notifications.cooldown_enabled)
            options.setdefault("cooldown_seconds", self.notifications.cooldown_seconds)
            options.setdefault("bbox_iou_threshold", self.notifications.bbox_iou_threshold)
            options.setdefault("timeout_seconds", self.notifications.timeout_seconds)
        elif module_name == "modules.output.rate_limiter":
            options.setdefault("input_topic", self.rate_limit.input_topic)
            options.setdefault("output_topic", self.rate_limit.output_topic)
            options.setdefault("max_events", self.rate_limit.max_events)
            options.setdefault("per_seconds", self.rate_limit.per_seconds)
            options.setdefault("key_field", self.rate_limit.key_field)
        elif module_name == "modules.dashboard.control_api":
            options.setdefault("host", self.control_api.host)
            options.setdefault("port", self.control_api.port)
            options.setdefault("serve_api", self.control_api.serve_api)
            options.setdefault("command_topic", self.control_api.command_topic)
            options.setdefault("config_topic", self.control_api.config_topic)
            options.setdefault("tls_enabled", self.control_api.tls.enabled)
            options.setdefault("tls_certfile", self.control_api.tls.certfile)
            options.setdefault("tls_keyfile", self.control_api.tls.keyfile)
            options.setdefault("tls_ca_certfile", self.control_api.tls.ca_certfile)
            options.setdefault("tls_require_client_cert", self.control_api.tls.require_client_cert)
        elif module_name == "modules.dashboard.telegram_bot":
            if "token" not in options or _needs_resolution(options["token"]):
                options["token"] = self.telegram.token
            options.setdefault("default_camera_id", self.camera.camera_id)
            options.setdefault("command_topic", self.control_api.command_topic)
            options.setdefault("health_topic", "status.health.summary")
            whitelist = options.get("user_whitelist")
            if whitelist is None or _needs_resolution(whitelist):
                options["user_whitelist"] = self.authentication.user_whitelist
            superuser = options.get("superuser_id")
            if superuser is None or _needs_resolution(superuser):
                options["superuser_id"] = self.authentication.superuser_id
            setup_password = options.get("setup_password")
            if setup_password is None or _needs_resolution(setup_password):
                options["setup_password"] = self.authentication.setup_password
            options.setdefault("allow_group_commands", self.telegram_security.allow_group_commands)
            options.setdefault("silent_unauthorized", self.telegram_security.silent_unauthorized)
            options.setdefault("command_rate_limit", self.telegram_rate_limiting.command_rate_limit)
            options.setdefault("send_typing_action", self.telegram_behavior.send_typing_action)
            options.setdefault("delete_old_menus", self.telegram_behavior.delete_old_menus)
            options.setdefault("command_timeout", self.telegram_behavior.command_timeout)
            options.setdefault("snapshot_timeout", self.telegram_behavior.snapshot_timeout)
        elif module_name == "modules.dashboard.websocket_gateway":
            options.setdefault("host", self.websocket_gateway.host)
            options.setdefault("port", self.websocket_gateway.port)
            options.setdefault("serve_http", self.websocket_gateway.serve_http)
            options.setdefault("tls_enabled", self.websocket_gateway.tls.enabled)
            options.setdefault("tls_certfile", self.websocket_gateway.tls.certfile)
            options.setdefault("tls_keyfile", self.websocket_gateway.tls.keyfile)
            options.setdefault("tls_ca_certfile", self.websocket_gateway.tls.ca_certfile)
            options.setdefault(
                "tls_require_client_cert", self.websocket_gateway.tls.require_client_cert
            )
            options.setdefault("topics", self.websocket_gateway.topics)
            options.setdefault("buffer_size", self.websocket_gateway.buffer_size)
            options.setdefault("idle_timeout_seconds", self.websocket_gateway.idle_timeout_seconds)

        return ModuleConfig(enabled=config.enabled, options=options)


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

    def _resolve_module_name(self, module: str | type[BaseModule] | BaseModule) -> str:
        if isinstance(module, BaseModule):
            return module.name
        if isinstance(module, str):
            return module
        return getattr(module, "name", module.__name__)

    def module_config_for(
        self, module: str | type[BaseModule] | BaseModule, *, camera_id: str | None = None
    ) -> ModuleConfig:
        """
        Convenient wrapper around ConfigSnapshot.module_config that accepts
        module names, classes, or instances.
        """
        module_name = self._resolve_module_name(module)
        return self._snapshot.module_config(module_name, camera_id=camera_id)

    def module_configs_for(
        self, module: str | type[BaseModule] | BaseModule, *, camera_id: str | None = None
    ) -> list[ModuleConfig]:
        """
        Return all applicable ModuleConfig objects for the requested module.
        """
        module_name = self._resolve_module_name(module)
        return self._snapshot.module_configs(module_name, camera_id=camera_id)

    def _build_snapshot(self, raw: dict[str, Any] | None = None) -> ConfigSnapshot:
        data = self._extract_snapshot_data(raw or self._settings.as_dict())
        try:
            return ConfigSnapshot.model_validate(data)
        except ValidationError as exc:
            raise ConfigError("Configuration validation failed") from exc

    def _extract_snapshot_data(self, raw: dict[str, Any]) -> dict[str, Any]:
        detection_section = _section(raw, "detection")
        system_section = _section(raw, "system")
        outputs_section = _section(raw, "outputs")
        dashboards_section = _section(raw, "dashboards")

        def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
            if not override:
                return dict(base)
            merged = dict(base)
            merged.update(override)
            return merged

        notifications_section = _section(detection_section, "notifications")
        # Clip settings can come from media_artifacts (legacy) or notifications.video (preferred)
        media_section = _section(detection_section, "media_artifacts")
        video_section = _section(notifications_section, "video")
        clip_data = _merge(media_section, _section(raw, "media_artifacts"))
        clip_data = _merge(clip_data, video_section)  # notifications.video takes precedence
        clip_data = _merge(clip_data, _section(raw, "clip"))

        data = {
            "camera": _section(raw, "camera"),
            "cameras": _section_list(raw, "cameras"),
            "detection": detection_section,
            "alert_pipeline": _merge(
                _section(detection_section, "alert_pipeline"), _section(raw, "alert_pipeline")
            ),
            "dedupe": _merge(_section(detection_section, "dedupe"), _section(raw, "dedupe")),
            "storage": _section(raw, "storage"),
            "notifications": _merge(notifications_section, _section(raw, "notifications")),
            "rate_limit": _merge(
                _section(outputs_section, "rate_limit"), _section(raw, "rate_limit")
            ),
            "zoning": _merge(_section(detection_section, "zoning"), _section(raw, "zoning")),
            "clip": clip_data,
            "advanced": _section(system_section, "advanced") or _section(raw, "advanced"),
            "analytics": _section(raw, "analytics"),
            "control_api": _section(system_section, "control_api") or _section(raw, "control_api"),
            "websocket_gateway": _merge(
                _section(dashboards_section, "websocket_gateway"),
                _section(raw, "websocket_gateway"),
            ),
            "telegram": _section(raw, "telegram"),
            "telegram_security": _section(system_section, "security") or _section(raw, "security"),
            "telegram_behavior": _section(system_section, "behavior") or _section(raw, "behavior"),
            "telegram_rate_limiting": _section(system_section, "rate_limiting")
            or _section(raw, "rate_limiting"),
            "authentication": _section(system_section, "authentication")
            or _section(raw, "authentication"),
            "resilience": _section(system_section, "resilience") or _section(raw, "resilience"),
            "outputs": _section_list(outputs_section, "modules") or _section_list(raw, "outputs"),
            "dashboards": _section_list(dashboards_section, "modules")
            or _section_list(raw, "dashboards"),
            "modules": _section_list(raw, "modules"),
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
    "ModuleManifestEntry",
    "ResilienceScenarioSettings",
    "ResilienceSettings",
    "S3SyncSettings",
    "StorageSettings",
    "WebsocketGatewaySettings",
    "ZoneDefinition",
    "ZoningSettings",
]
