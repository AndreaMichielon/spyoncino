"""
Collection of modular Spyoncino components grouped by responsibility.

Only a subset of modules are implemented for the initial migration phase.
"""

from .event.gif_builder import GifBuilder
from .event.snapshot_writer import SnapshotWriter
from .input.camera_sim import CameraSimulator
from .input.rtsp_camera import RtspCamera
from .output.telegram_notifier import TelegramNotifier
from .process.motion_detector import MotionDetector
from .process.yolo_detector import YoloDetector
from .status.prometheus_exporter import PrometheusExporter

__all__ = [
    "CameraSimulator",
    "GifBuilder",
    "MotionDetector",
    "PrometheusExporter",
    "RtspCamera",
    "SnapshotWriter",
    "TelegramNotifier",
    "YoloDetector",
]
