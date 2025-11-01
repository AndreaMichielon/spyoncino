"""
Video Capture Module

Professional OpenCV VideoCapture wrapper providing reliable camera access
with error handling, resource management, and configuration support.
"""

import cv2
import logging
import numpy as np
from typing import Optional, Iterator, Union, Dict, Any

class Capture:
    """
    Professional video capture wrapper for OpenCV VideoCapture.
    
    Provides reliable camera access with proper error handling,
    resource management, and optional configuration.
    """
    
    def __init__(self, source: Union[int, str] = 0, **kwargs):
        """
        Initialize capture device.
        
        Args:
            source: Camera index (int) or video file path (str)
            **kwargs: Additional VideoCapture properties (width, height, fps, etc.)
            
        Raises:
            ValueError: If source is invalid type or negative camera index
        """
        if isinstance(source, int) and source < 0:
            raise ValueError("Camera index must be non-negative")
        if isinstance(source, str) and not source.strip():
            raise ValueError("Video file path cannot be empty")
            
        self.source = source
        self.capture: Optional[cv2.VideoCapture] = None
        self.config = kwargs
        self.logger = logging.getLogger(self.__class__.__name__)
        
    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with guaranteed cleanup."""
        self.disconnect()
        
    def connect(self) -> bool:
        """
        Open the VideoCapture device with error handling.
        
        Returns:
            bool: True if connection successful, False otherwise
        """
        try:
            if self.capture is None or not self.capture.isOpened():
                self.capture = cv2.VideoCapture(self.source)
                
                if not self.capture.isOpened():
                    self.logger.error(f"Failed to open capture source: {self.source}")
                    return False
                
                # Apply configuration if provided
                self._apply_config()
                self.logger.info(f"Connected to capture source: {self.source}")
                
            return True
                
        except (cv2.error, OSError, RuntimeError) as e:
            self.logger.error(f"Connection error: {e}")
            return False
    
    def _apply_config(self) -> None:
        """Apply configuration properties to capture device."""
        if not self.capture or not self.config:
            return
            
        property_map = {
            'width': cv2.CAP_PROP_FRAME_WIDTH,
            'height': cv2.CAP_PROP_FRAME_HEIGHT,
            'fps': cv2.CAP_PROP_FPS,
            'brightness': cv2.CAP_PROP_BRIGHTNESS,
            'contrast': cv2.CAP_PROP_CONTRAST,
            'exposure': cv2.CAP_PROP_EXPOSURE,
        }
        
        for key, value in self.config.items():
            if key in property_map:
                try:
                    success = self.capture.set(property_map[key], value)
                    if success:
                        self.logger.debug(f"Set {key}={value}")
                    else:
                        self.logger.warning(f"Failed to set {key}={value}")
                except (cv2.error, ValueError) as e:
                    self.logger.warning(f"Invalid config {key}={value}: {e}")
        
    @property
    def is_connected(self) -> bool:
        """Check if capture device is active and ready."""
        return self.capture is not None and self.capture.isOpened()
        
    def disconnect(self) -> None:
        """Release capture device and cleanup resources."""
        if self.capture is not None:
            try:
                self.capture.release()
                self.logger.info("Capture device disconnected")
            except (cv2.error, RuntimeError) as e:
                self.logger.error(f"Error during disconnect: {e}")
            finally:
                self.capture = None
    
    def grab(self) -> Optional[np.ndarray]:
        """
        Capture a single frame.
        
        Returns:
            np.ndarray: Frame data if successful, None otherwise
        """
        if not self.is_connected:
            return None
            
        try:
            ret, frame = self.capture.read()
            if not ret or frame is None:
                self.logger.warning("Camera disconnected or stream lost")
                self.disconnect()
                return None
            return frame
        except (cv2.error, RuntimeError) as e:
            self.logger.error(f"Frame capture error: {e}")
            self.disconnect() 
            return None
    
    def grab_multiple(self, count: int) -> list[Optional[np.ndarray]]:
        """
        Capture multiple frames efficiently.
        
        Args:
            count: Number of frames to capture
            
        Returns:
            list: List of frames (may contain None for failed captures)
            
        Raises:
            ValueError: If count is not positive
        """
        if count <= 0:
            raise ValueError("Frame count must be positive")
        return [self.grab() for _ in range(count)]
    
    def stream(self) -> Iterator[np.ndarray]:
        """
        Generator that yields frames until disconnect or failure.
        
        Yields:
            np.ndarray: Video frames
        """
        while self.is_connected:
            frame = self.grab()
            if frame is None:
                break
            yield frame
        
    def get_info(self) -> Dict[str, Any]:
        """
        Get camera/video information.
        
        Returns:
            dict: Camera properties and settings
        """
        if not self.is_connected:
            return {}
            
        try:
            info = {
                'width': int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                'height': int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                'fps': self.capture.get(cv2.CAP_PROP_FPS),
                'backend': self.capture.getBackendName(),
            }
            
            # Only add frame count for video files (not live cameras)
            if isinstance(self.source, str):
                info['total_frames'] = int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT))
                info['codec'] = int(self.capture.get(cv2.CAP_PROP_FOURCC))
                
            return info
        except (cv2.error, AttributeError) as e:
            self.logger.error(f"Error getting camera info: {e}")
            return {}
    
    def __repr__(self) -> str:
        """String representation for debugging."""
        status = "connected" if self.is_connected else "disconnected"
        return f"Capture(source={self.source}, status={status})"