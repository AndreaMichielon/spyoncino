"""
Collection of modular Spyoncino components grouped by responsibility.

Only a subset of modules are implemented for the initial migration phase.
"""

from .event.snapshot_writer import SnapshotWriter
from .input.camera_sim import CameraSimulator
from .output.telegram_notifier import TelegramNotifier
from .process.motion_detector import MotionDetector

__all__ = [
    "CameraSimulator",
    "MotionDetector",
    "SnapshotWriter",
    "TelegramNotifier",
]
