"""
Collection of modular Spyoncino components grouped by responsibility.

Only a subset of modules are implemented for the initial migration phase.
"""

from .analytics.db_logger import AnalyticsDbLogger
from .analytics.event_logger import AnalyticsEventLogger

try:  # pragma: no cover - optional dependency
    from .dashboard.control_api import ControlApi
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    ControlApi = None

try:  # pragma: no cover - optional dependency
    from .dashboard.telegram_bot import TelegramControlBot
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    TelegramControlBot = None

try:  # pragma: no cover - optional dependency
    from .dashboard.websocket_gateway import WebsocketGateway
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    WebsocketGateway = None
from .event.clip_builder import ClipBuilder
from .event.deduplicator import EventDeduplicator
from .event.gif_builder import GifBuilder
from .event.snapshot_writer import SnapshotWriter
from .input.camera_sim import CameraSimulator
from .input.rtsp_camera import RtspCamera
from .input.usb_camera import UsbCamera
from .output.rate_limiter import RateLimiter
from .output.telegram_notifier import TelegramNotifier
from .process.detection_event_router import DetectionEventRouter
from .process.motion_detector import MotionDetector
from .process.yolo_detector import YoloDetector
from .process.zoning_filter import ZoningFilter
from .status.prometheus_exporter import PrometheusExporter
from .status.resilience_tester import ResilienceTester
from .storage.retention import StorageRetention
from .storage.s3_uploader import S3ArtifactUploader

__all__ = [
    "AnalyticsDbLogger",
    "AnalyticsEventLogger",
    "CameraSimulator",
    "ClipBuilder",
    "ControlApi",
    "DetectionEventRouter",
    "EventDeduplicator",
    "GifBuilder",
    "MotionDetector",
    "PrometheusExporter",
    "RateLimiter",
    "ResilienceTester",
    "RtspCamera",
    "S3ArtifactUploader",
    "SnapshotWriter",
    "StorageRetention",
    "TelegramControlBot",
    "TelegramNotifier",
    "UsbCamera",
    "WebsocketGateway",
    "YoloDetector",
    "ZoningFilter",
]
