"""Input modules responsible for acquiring frames or streams."""

from .camera_sim import CameraSimulator
from .rtsp_camera import RtspCamera
from .usb_camera import UsbCamera

__all__ = ["CameraSimulator", "RtspCamera", "UsbCamera"]
