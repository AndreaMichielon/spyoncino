"""
Contracts and payload schemas for the modular Spyoncino architecture.

The goal is to provide strongly typed interfaces that modules use to
interact with the event bus and orchestrator. These definitions cover the
Week 1 scope described in `TODO_ARCHITECTURE.md` by introducing
foundational payloads and lifecycle hooks.
"""

from __future__ import annotations

import abc
import datetime as dt
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class BasePayload(BaseModel):
    """Base class for all bus payloads."""

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: str = Field(
        default="1.0.0", description="Semantic version of the payload schema."
    )


class Frame(BasePayload):
    """Image frame captured by an input module."""

    camera_id: str
    timestamp_utc: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(tz=dt.UTC),
        description="Capture timestamp in UTC.",
    )
    sequence_id: int | None = Field(
        default=None, description="Optional monotonically increasing frame number."
    )
    data_ref: str | None = Field(
        default=None,
        description="Reference to the binary payload (file path, object store key, etc.).",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Arbitrary metadata from the capture device."
    )
    image_bytes: bytes | None = Field(
        default=None,
        description="Optional encoded image bytes (PNG/JPEG) for downstream modules.",
    )
    content_type: str | None = Field(
        default=None,
        description="MIME type describing `image_bytes` payload.",
    )


class DetectionEvent(BasePayload):
    """Motion or object detection emitted by processing modules."""

    camera_id: str
    detector_id: str
    timestamp_utc: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(tz=dt.UTC),
        description="Detection timestamp in UTC.",
    )
    triggered: bool = Field(
        default=True, description="Whether the detector identified a positive condition."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    frame_ref: str | None = Field(
        default=None,
        description="Identifier linking back to the source frame.",
    )
    attributes: dict[str, Any] = Field(
        default_factory=dict, description="Detector-specific metadata."
    )


class SnapshotArtifact(BasePayload):
    """Snapshot or media artifact persisted to disk and ready for notifications."""

    camera_id: str
    artifact_path: str = Field(description="Absolute path to the artifact on disk.")
    content_type: str = Field(default="image/png")
    metadata: dict[str, Any] = Field(default_factory=dict)


class MediaArtifact(BasePayload):
    """Generic media artifact such as MP4 clips or GIFs."""

    camera_id: str
    artifact_path: str = Field(description="Absolute path to the artifact on disk.")
    media_kind: str = Field(default="clip", description="Human readable media type.")
    content_type: str = Field(default="video/mp4")
    metadata: dict[str, Any] = Field(default_factory=dict)


class BusStatus(BasePayload):
    """Telemetry snapshot emitted by the event bus on `status.bus`."""

    queue_depth: int = Field(ge=0, description="Current number of queued events.")
    queue_capacity: int = Field(gt=0, description="Maximum queue capacity.")
    subscriber_count: int = Field(ge=0, description="Total registered handlers.")
    topic_count: int = Field(ge=0, description="Unique topics with subscribers.")
    published_total: int = Field(ge=0, description="Cumulative published events.")
    processed_total: int = Field(ge=0, description="Cumulative dispatched events.")
    dropped_total: int = Field(
        ge=0, description="Events dropped due to queue pressure or shutdown."
    )
    lag_seconds: float = Field(
        ge=0.0,
        description="Approximate lag between last publish and last dispatch completion.",
    )
    watermark: str = Field(
        default="normal",
        description="Watermark classification (normal/high/critical).",
    )


class HealthStatus(BaseModel):
    """Structured health report for modules."""

    model_config = ConfigDict(extra="allow", frozen=True)

    status: str = Field(description="Health classification such as healthy/degraded/error.")
    details: dict[str, Any] = Field(default_factory=dict)


class HealthSummary(BasePayload):
    """Aggregated health report emitted on `status.health.summary`."""

    status: str = Field(description="Overall classification.")
    modules: dict[str, HealthStatus] = Field(default_factory=dict)


class ModuleConfig(BaseModel):
    """Baseline configuration contract applied to all modules."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = Field(default=True)
    options: dict[str, Any] = Field(
        default_factory=dict, description="Arbitrary module configuration."
    )


@runtime_checkable
class EventHandler(Protocol):
    """Callable type for bus subscribers."""

    async def __call__(self, topic: str, payload: BasePayload) -> None: ...


if TYPE_CHECKING:
    from .bus import EventBus


class BaseModule(abc.ABC):
    """
    Abstract base class for all modular components.

    Modules receive an event bus instance and are responsible for
    subscribing to topics during `start`.
    """

    name: str
    default_topics: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._configured = False
        self._config = ModuleConfig()
        self._bus: EventBus | None = None

    @property
    def bus(self) -> EventBus:
        if self._bus is None:
            raise RuntimeError(f"{self.__class__.__name__} has not been attached to an EventBus.")
        return self._bus

    def set_bus(self, bus: EventBus) -> None:
        """Attach the shared event bus instance to the module."""
        self._bus = bus

    async def configure(self, config: ModuleConfig) -> None:
        """Apply the provided configuration prior to module start."""
        self._config = config
        self._configured = True

    @abc.abstractmethod
    async def start(self) -> None:
        """Begin processing by registering bus subscriptions or scheduling tasks."""

    async def stop(self) -> None:
        """
        Optional hook to release resources.

        Base implementation is a no-op so subclasses can override only
        when needed without being forced to mark the method abstract.
        """
        return None

    async def health(self) -> HealthStatus:
        """Return a basic health status; modules can override for richer diagnostics."""
        status = "healthy" if self._configured else "degraded"
        return HealthStatus(status=status, details={"configured": self._configured})


class ControlCommand(BasePayload):
    """Payload emitted by dashboards or APIs to control modules."""

    command: str = Field(description="Command identifier, e.g. camera.state")
    camera_id: str | None = Field(default=None)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ConfigUpdate(BasePayload):
    """Request to apply configuration changes or reload from disk."""

    source: str = Field(default="unknown")
    changes: dict[str, Any] = Field(default_factory=dict)
    reload: bool = Field(
        default=False,
        description="When true the config service should reload from disk instead of applying changes.",
    )


class ConfigSnapshotPayload(BasePayload):
    """Published whenever a new configuration snapshot becomes active."""

    data: dict[str, Any] = Field(default_factory=dict, description="Snapshot dictionary view.")


class StorageStats(BasePayload):
    """Filesystem usage snapshot emitted by the retention module."""

    root: str = Field(description="Root directory being managed.")
    total_gb: float = Field(ge=0.0)
    used_gb: float = Field(ge=0.0)
    free_gb: float = Field(ge=0.0)
    usage_percent: float = Field(ge=0.0, le=100.0)
    files_deleted: int = Field(default=0, ge=0)
    aggressive: bool = Field(
        default=False, description="Whether the last cleanup used the aggressive policy."
    )
    warning: bool = Field(
        default=False,
        description="True when disk space dropped below the configured threshold.",
    )
    artifacts: dict[str, int] = Field(
        default_factory=dict,
        description="Per-directory artifact counts after the cleanup run.",
    )
