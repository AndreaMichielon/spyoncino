"""
Collection of modular Spyoncino components grouped by responsibility.

Only a subset of modules are implemented for the initial migration phase.
"""

from .dashboard.control_api import ControlApi
from .dashboard.telegram_bot import TelegramControlBot
from .event.clip_builder import ClipBuilder
from .event.deduplicator import EventDeduplicator
from .event.gif_builder import GifBuilder
from .event.snapshot_writer import SnapshotWriter
from .input.camera_sim import CameraSimulator
from .input.rtsp_camera import RtspCamera
from .input.usb_camera import UsbCamera
from .output.rate_limiter import RateLimiter
from .output.telegram_notifier import TelegramNotifier
from .process.motion_detector import MotionDetector
from .process.yolo_detector import YoloDetector
from .process.zoning_filter import ZoningFilter
from .status.prometheus_exporter import PrometheusExporter

__all__ = [
    "CameraSimulator",
    "ClipBuilder",
    "ControlApi",
    "EventDeduplicator",
    "GifBuilder",
    "MotionDetector",
    "PrometheusExporter",
    "RateLimiter",
    "RtspCamera",
    "SnapshotWriter",
    "TelegramControlBot",
    "TelegramNotifier",
    "UsbCamera",
    "YoloDetector",
    "ZoningFilter",
]
