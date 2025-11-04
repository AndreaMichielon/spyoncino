"""
AI-Powered Security System Module

Core security system providing motion detection, person recognition,
and event processing with YOLO-based AI detection and background processing.
"""

import cv2
import time
import logging
import torch
import imageio
import threading
import shutil
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Union, Callable, Dict, Any
from concurrent.futures import ThreadPoolExecutor, Future
from ultralytics import YOLO

from spyoncino.capture import Capture

class SecuritySystem:
    """
    Professional AI-powered security system with motion detection and person recognition.
    
    Features:
    - Motion detection using background subtraction
    - YOLO-based person detection with GPU optimization
    - Async GIF generation for notifications
    - Configurable detection parameters
    - Comprehensive logging and error handling
    """
    
    def __init__(
        self, 
        capture: Capture,
        event_folder: str = "events",
        config_dir: str = "config",
        interval: float = 1.0,
        record_frames: int = 30,
        confidence: float = 0.25,
        max_batch_size: int = 32,
        motion_threshold: int = 2,
        gif_fps: int = 15,
        max_gif_frames: int = 20,
        person_cooldown_seconds: float = 15.0,
        bbox_overlap_threshold: float = 0.6,
        person_timeout_seconds: float = 5.0
    ):
        """
        Initialize the security system.
        
        Args:
            capture: Capture instance for video input
            event_folder: Directory to store recorded events
            config_dir: Directory containing configuration and model files
            interval: Time between motion checks (seconds)
            record_frames: Number of frames to record per event
            confidence: YOLO detection confidence threshold
            max_batch_size: Maximum batch size for YOLO processing
            motion_threshold: Motion detection sensitivity
            gif_fps: Frame rate for generated GIFs
            max_gif_frames: Maximum frames in GIF (for size optimization)
        """
        if not isinstance(capture, Capture):
            raise TypeError("capture must be a Capture instance")
        if interval <= 0:
            raise ValueError("interval must be positive")
        if not 0.1 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0.1 and 1.0")
        if record_frames <= 0:
            raise ValueError("record_frames must be positive")
        if max_batch_size <= 0:
            raise ValueError("max_batch_size must be positive") 
        if motion_threshold < 0:
            raise ValueError("motion_threshold must be non-negative")
        if gif_fps <= 0:
            raise ValueError("gif_fps must be positive")
        if max_gif_frames <= 0:
            raise ValueError("max_gif_frames must be positive")

        # Core components
        self.capture = capture
        self.config_dir = Path(config_dir)
        self.interval = interval
        self.record_frames = record_frames
        self.confidence = confidence
        self.max_batch_size = max_batch_size
        self.motion_threshold = motion_threshold
        self.gif_fps = gif_fps
        self.max_gif_frames = max_gif_frames
        self.event_folder = event_folder

        # Logging
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info(f"Security system initialized with max_batch_size={self.max_batch_size}")
        
        # State management
        self.is_active = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Computer vision components
        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(detectShadows=True)
        self._initialize_yolo()
        
        # Anti-spam detection parameters
        self.person_cooldown_seconds = person_cooldown_seconds
        self.bbox_overlap_threshold = bbox_overlap_threshold
        self.person_timeout_seconds = person_timeout_seconds
        self.last_person_detection = 0
        self.last_person_bbox = None
        self.last_person_time = 0

        # Background processing
        self.gif_executor = ThreadPoolExecutor(
            max_workers=2, 
            thread_name_prefix="SecurityGifProcessor"
        )
        self._pending_gifs: List[Future] = []

        #For event logging
        self._error_callback = None
    
    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for error logging."""
        self._error_callback = callback

    def _initialize_yolo(self) -> None:
        """Initialize and warm up the YOLO model."""
        try:
            # Ensure config directory exists
            self.config_dir.mkdir(parents=True, exist_ok=True)
            model_path = self.config_dir / "yolov8n.pt"
            
            # Download model if missing
            if not model_path.exists():
                self.logger.info(f"Model not found in {model_path}, downloading...")
                
                # Check ultralytics cache first
                try:
                    from ultralytics.utils import WEIGHTS_DIR
                    cached = Path(WEIGHTS_DIR) / "yolov8n.pt"
                    if cached.exists():
                        shutil.copy2(str(cached), str(model_path))
                        self.logger.info(f"Model copied from cache to {model_path}")
                    else:
                        raise FileNotFoundError("Not in cache")
                except (ImportError, FileNotFoundError):
                    # Download via ultralytics
                    self.logger.info("Downloading model via ultralytics...")
                    temp_model = YOLO("yolov8n.pt")  # Downloads to cache
                    
                    # Copy from cache to config
                    try:
                        from ultralytics.utils import WEIGHTS_DIR
                        cached = Path(WEIGHTS_DIR) / "yolov8n.pt"
                        if cached.exists():
                            shutil.copy2(str(cached), str(model_path))
                            self.logger.info(f"Model saved to {model_path}")
                        else:
                            self.logger.warning(f"Using model from ultralytics cache")
                            model_path = cached
                    except Exception as e:
                        self.logger.error(f"Failed to locate downloaded model: {e}")
                        raise RuntimeError(f"Could not setup YOLO model: {e}")
            
            # Load model from config directory
            self.model = YOLO(str(model_path))
            
            # Warmup with dummy data
            dummy_frame = np.zeros((224, 224, 3), dtype=np.uint8)
            self.model.predict(source=dummy_frame, verbose=False)
            
            # Optimize batch size for available VRAM
            self.batch_size = self._optimize_batch_size()
            
            self.logger.info(f"YOLO model loaded: {model_path}")
            
        except (ImportError, OSError, RuntimeError) as e:
            self.logger.error(f"Failed to initialize YOLO: {e}")
            raise RuntimeError(f"YOLO initialization failed: {e}")
    
    def _optimize_batch_size(
        self, 
        frame_shape: Tuple[int, int, int] = (224, 224, 3), 
        memory_reserve: float = 0.2
    ) -> int:
        """
        Optimize batch size based on available GPU memory.
        
        Args:
            frame_shape: Expected input frame dimensions
            memory_reserve: Fraction of memory to keep free
            
        Returns:
            Optimal batch size for current hardware
        """
        if not torch.cuda.is_available():
            return 1
            
        try:
            free_memory, _ = torch.cuda.mem_get_info()
            free_gb = free_memory / (1024**3)
            
            # Estimate memory per frame (rough calculation)
            h, w, c = frame_shape
            bytes_per_frame = h * w * c * 4  # 4 bytes per float32
            gb_per_frame = bytes_per_frame / (1024**3)
            
            # Calculate optimal batch size
            available_memory = free_gb * (1 - memory_reserve)
            estimated_batch_size = int(available_memory / gb_per_frame)
            
            # Apply constraints
            optimal_batch_size = max(1, min(estimated_batch_size, self.max_batch_size))
            
            self.logger.debug(f"GPU memory optimization: {free_gb:.1f}GB free, batch_size={optimal_batch_size}")
            return optimal_batch_size
            
        except (RuntimeError, torch.cuda.CudaError) as e:
            self.logger.warning(f"Batch size optimization failed: {e}")
            return 1
    
    def _calculate_bbox_iou(self, bbox1: np.ndarray, bbox2: np.ndarray) -> float:
        """Calculate Intersection over Union (IoU) of two bounding boxes."""
        if bbox1.shape[0] < 4 or bbox2.shape[0] < 4:
            return 0.0

        x1_max = max(bbox1[0], bbox2[0])
        y1_max = max(bbox1[1], bbox2[1])
        x2_min = min(bbox1[2], bbox2[2])
        y2_min = min(bbox1[3], bbox2[3])
        
        if x2_min <= x1_max or y2_min <= y1_max:
            return 0.0
        
        intersection = (x2_min - x1_max) * (y2_min - y1_max)
        area1 = (bbox1[2] - bbox1[0]) * (bbox1[3] - bbox1[1])
        area2 = (bbox2[2] - bbox2[0]) * (bbox2[3] - bbox2[1])
        union = area1 + area2 - intersection
        
        return intersection / union if union > 0 else 0.0

    def _should_suppress_person_detection(self, current_bbox: np.ndarray) -> bool:
        """Determine if person detection should be suppressed due to anti-spam rules."""
        current_time = time.time()
        
        # Check global cooldown
        if current_time - self.last_person_detection < self.person_cooldown_seconds:
            return True
        
        # Reset bbox tracking if timeout exceeded
        if current_time - self.last_person_time > self.person_timeout_seconds:
            self.last_person_bbox = None
        
        # Check spatial overlap with previous detection
        if self.last_person_bbox is not None:
            overlap = self._calculate_bbox_iou(current_bbox, self.last_person_bbox)
            if overlap > self.bbox_overlap_threshold:
                return True
        
        return False

    def _update_person_tracking(self, bbox: np.ndarray) -> None:
        """Update person detection tracking state."""
        current_time = time.time()
        self.last_person_detection = current_time
        self.last_person_time = current_time
        self.last_person_bbox = bbox.copy()

    def _cleanup_resources(self) -> None:
        """Clean up system resources."""
        if hasattr(self, 'gif_executor'):
            self.gif_executor.shutdown(wait=True)
        if hasattr(self, 'model'):
            # Clear CUDA cache if using GPU
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def start(self) -> bool:
        """
        Start the security monitoring system.
        
        Returns:
            bool: True if started successfully
        """
        if self.is_active:
            self.logger.warning("Security system already active")
            return True
            
        if not self.capture.connect():
            self.logger.error("Failed to connect to capture device")
            return False
            
        self.is_active = True
        self._stop_event.clear()
        
        # Start monitoring thread
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="SecurityMonitor",
            daemon=True
        )
        self._monitor_thread.start()
        
        self.logger.info("Security monitoring started")
        return True
    
    def stop(self) -> None:
        """Stop the security monitoring system."""
        if not self.is_active:
            return
            
        self.logger.info("Stopping security system...")
        self.is_active = False
        self._stop_event.set()
        
        # Wait for monitor thread to finish
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5.0)
            
        # Cleanup resources
        self.capture.disconnect()
        
        # Wait for pending GIF operations
        self._wait_for_pending_gifs()

        self._cleanup_resources()
        
        self.logger.info("Security system stopped")
    
    def _monitor_loop(self) -> None:
        """Main monitoring loop (runs in separate thread)."""
        consecutive_failures = 0
        max_failures = 5
        
        while self.is_active and not self._stop_event.is_set():
            try:
                # Check connection
                if not self.capture.is_connected:
                    self.logger.warning("Capture disconnected, attempting reconnection...")
                    if not self.capture.connect():
                        consecutive_failures += 1
                        if consecutive_failures >= max_failures:
                            self.logger.error("Max connection failures reached, stopping monitor")
                            break
                        time.sleep(self.interval * 2)
                        continue
                    consecutive_failures = 0
                
                # Grab frame and check for motion
                frame = self.capture.grab()
                if frame is not None and self._detect_motion(frame):
                    self.logger.info("Motion detected, recording event...")
                    self._handle_motion_event()
                
                # Cleanup completed GIF operations
                self._cleanup_completed_gifs()
                
                # Wait for next check
                self._stop_event.wait(timeout=self.interval)
                
            except KeyboardInterrupt:
                self.logger.info("Monitor loop interrupted by user")
                break
            except Exception as e:
                self.logger.error(f"Monitor loop error: {e}", exc_info=True)
                
                if self._error_callback and callable(self._error_callback):
                    self._error_callback(f"Security system error: {str(e)[:100]}")

                consecutive_failures += 1
                if consecutive_failures >= max_failures:
                    self.logger.error("Too many consecutive failures, stopping monitor")
                    break
                time.sleep(self.interval)
    
    def _detect_motion(self, frame: np.ndarray) -> bool:
        """
        Detect motion in frame using background subtraction with visual feedback.
        
        Args:
            frame: Input video frame
            
        Returns:
            bool: True if motion detected above threshold
        """
        try:
            # Apply background subtraction
            fg_mask = self.bg_subtractor.apply(frame)
            motion_pixels = cv2.countNonZero(fg_mask)
            total_pixels = fg_mask.shape[0] * fg_mask.shape[1]
            motion_percent = int((motion_pixels / total_pixels) * 100)
            
            return motion_percent > self.motion_threshold
            
        except (cv2.error, AttributeError) as e:
            self.logger.error(f"Motion detection error: {e}")
            return False
    
    def _add_motion_overlay(self, frame: np.ndarray, fg_mask: np.ndarray, motion_score: int) -> None:
        """Add professional motion visualization overlay to frame - fully responsive."""
        try:
            # Visual constants
            COLORS = {
                'motion': (0, 100, 255),
                'bg': (40, 40, 40),
                'text': (255, 255, 255),
                'label': (180, 180, 180),
                'active': (0, 255, 128),
                'triggered': (0, 165, 255)
            }
            
            # Apply motion overlay
            motion_overlay = np.zeros_like(frame)
            motion_overlay[fg_mask > 0] = COLORS['motion']
            cv2.addWeighted(frame, 0.75, motion_overlay, 0.25, 0, frame)
            
            # Scale all dimensions based on frame size
            h, w = frame.shape[:2]
            scale_factor = min(h, w) / 320  # Base scale on 480p as reference
            
            # Enhanced scaling with minimum guarantees
            font_scale = max(0.6, min(2.5, 0.7 * scale_factor))
            thickness = max(2, int(font_scale * 2.5))

            # Scale all UI elements proportionally
            margin = max(15, int(20 * scale_factor))
            padding = max(10, int(15 * scale_factor))
            panel_w = max(200, int(240 * scale_factor))
            panel_h = max(90, int(100 * scale_factor))
            line_height = max(25, int(35 * scale_factor))
            dot_radius = max(3, int(6 * scale_factor))

            # Panel position
            x, y = margin, margin
            
            # Status and colors
            is_triggered = motion_score > self.motion_threshold
            status = "MOTION DETECTED" if is_triggered else "MONITORING"
            accent = COLORS['triggered'] if is_triggered else COLORS['active']
            
            # Draw semi-transparent panel
            overlay = frame.copy()
            cv2.rectangle(overlay, (x, y), (x + panel_w, y + panel_h), COLORS['bg'], -1)
            cv2.addWeighted(frame, 0.3, overlay, 0.7, 0, frame)
            cv2.rectangle(frame, (x, y), (x + panel_w, y + panel_h), accent, max(1, int(scale_factor)))
            
            # Status with dot indicator
            text_x = x + padding
            dot_x = text_x + int(5 * scale_factor)
            status_y = y + padding + int(15 * scale_factor)
            
            cv2.circle(frame, (dot_x, status_y - dot_radius), dot_radius, accent, -1)
            cv2.putText(frame, status, (dot_x + dot_radius + int(5 * scale_factor), status_y), 
                    cv2.FONT_HERSHEY_SIMPLEX, font_scale, accent, thickness, cv2.LINE_AA)
            
            # Motion & threshold info
            info_scale = font_scale * 0.8
            info_thickness = max(1, thickness - 1)
            
            motion_y = status_y + (2*line_height//3)
            cv2.putText(frame, f"Threshold: {self.motion_threshold}%", (text_x, motion_y),
                    cv2.FONT_HERSHEY_SIMPLEX, info_scale, COLORS['text'], info_thickness, cv2.LINE_AA)
            
            threshold_y = motion_y + (2*line_height//3)
            cv2.putText(frame, f"Motion: {motion_score}%", (text_x, threshold_y),
                    cv2.FONT_HERSHEY_SIMPLEX, info_scale, COLORS['label'], info_thickness, cv2.LINE_AA)
            
            # Progress bar
            bar_margin = int(10 * scale_factor)
            bar_height = max(2, int(3 * scale_factor))
            bar_x = x + bar_margin
            bar_y = threshold_y + (line_height//3)
            bar_w = panel_w - (bar_margin * 2)
            
            cv2.rectangle(frame, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_height), (60, 60, 60), -1)
            
            progress = min(1.0, motion_score / (self.motion_threshold * 2))
            if progress > 0:
                cv2.rectangle(frame, (bar_x, bar_y), 
                            (bar_x + int(bar_w * progress), bar_y + bar_height), accent, -1)
           
        except (cv2.error, ValueError) as e:
            self.logger.error(f"Motion overlay error: {e}")

    def _handle_motion_event(self) -> None:
        """
        Handle detected motion event by recording and processing frames.
        
        Uses streaming generator pattern with optimal early-exit:
        1. Yields frames one at a time during capture
        2. Processes person detection in mini-batches (with caching)
        3. Early exit when person found (skips YOLO on remaining frames)
        4. Applies overlays: cached for processed, fresh YOLO for remaining
        
        Raises:
            RuntimeError: If frame capture fails
        """
        try:
            # Stream frames and detect person with early exit
            has_person, frames, cached_detections, frames_processed = self._collect_and_detect_person()
            
            if not frames:
                self.logger.warning("No frames captured for event")
                return
            
            # Apply overlays: uses cache + processes remaining frames
            processed_frames = self._apply_frame_overlays(
                frames, has_person, cached_detections, frames_processed
            )
            
            # Cleanup raw frames immediately
            del frames
            
            # Queue for complete processing
            event_data = {
                'frames': processed_frames,
                'has_person': has_person,
                'timestamp': datetime.now().strftime("%Y%m%d_%H%M%S"),
                'event_type': "person" if has_person else "motion"
            }
            
            self._queue_complete_event_processing(event_data)
            
        except Exception as e:
            self.logger.error(f"Event handling error: {e}", exc_info=True)

    def _queue_complete_event_processing(self, event_data: dict) -> None:
        """Queue event for complete background processing."""
        future = self.gif_executor.submit(self._process_complete_event, event_data)
        self._pending_gifs.append(future)
        
        self.logger.debug(f"Queued {event_data['event_type']} event for processing")

    def _process_complete_event(self, event_data: dict) -> Optional[str]:
        """
        Complete event processing: create GIF and trigger notifications.
        
        This runs in background thread and ensures atomicity:
        - Either everything succeeds and handlers get valid file paths
        - Or nothing happens and no broken notifications are sent
        """
        try:
            # Create the GIF file
            gif_path = self._create_gif_synchronously(
                frames=event_data['frames'],
                event_type=event_data['event_type'], 
                timestamp=event_data['timestamp'],
                output_dir=self.event_folder
            )
            
            if not gif_path or not gif_path.exists():
                self.logger.warning(f"Failed to create GIF for {event_data['event_type']} event")
                return None
            
            # File is guaranteed to exist - now trigger handlers
            self._trigger_event_handlers(event_data, str(gif_path))
            
            return str(gif_path)
            
        except Exception as e:
            self.logger.error(f"Complete event processing failed: {e}", exc_info=True)
            return None

    def _create_gif_synchronously(
        self, 
        frames: List[np.ndarray], 
        event_type: str, 
        timestamp: str,
        output_dir: Union[str, Path] = "events"
    ) -> Optional[Path]:
        """
        Create GIF synchronously and return path only if successful.
        
        Returns:
            Path object if GIF created successfully, None otherwise
        """
        try:
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            
            gif_path = output_path / f"{event_type}_{timestamp}.gif"
            
            success = self._generate_gif(frames, gif_path, self.gif_fps)
            
            return gif_path if success else None
            
        except Exception as e:
            self.logger.error(f"Synchronous GIF creation failed: {e}")
            return None

    def _trigger_event_handlers(self, event_data: dict, gif_path: str) -> None:
        """
        Trigger appropriate event handlers with guaranteed-valid file path.
        
        Args:
            event_data: Event information dictionary
            gif_path: Absolute path to created GIF file
        """
        try:
            if event_data['has_person'] and hasattr(self, 'on_person') and callable(self.on_person):
                self.logger.info(f"Triggering person event handler: {gif_path}")
                self.on_person(gif_path)
                
            elif hasattr(self, 'on_motion') and callable(self.on_motion):
                self.logger.info(f"Triggering motion event handler: {gif_path}")
                self.on_motion(gif_path)
                
            else:
                self.logger.debug(f"No handlers registered for {event_data['event_type']} event")
                
        except Exception as e:
            self.logger.error(f"Event handler execution failed: {e}", exc_info=True)

    def set_person_handler(self, handler: Callable[[str], None]) -> None:
        """Register handler for person detection events."""
        if not callable(handler):
            raise TypeError("handler must be callable")
        self.on_person = handler
        self.logger.info("Person event handler registered")

    def set_motion_handler(self, handler: Callable[[str], None]) -> None:
        """Register handler for motion detection events."""
        if not callable(handler):
            raise TypeError("handler must be callable")
        self.on_motion = handler
        self.logger.info("Motion event handler registered")
        
    def _record_frames_streaming(self):
        """
        Generator that yields frames one at a time for streaming processing.
        
        Yields:
            np.ndarray: Individual frames as they are captured
        """
        for _ in range(self.record_frames):
            if not self.is_active:
                break
            
            frame = self.capture.grab()
            if frame is not None:
                yield frame.copy()
            else:
                time.sleep(0.01)
    
    def _collect_and_detect_person(self) -> Tuple[bool, List[np.ndarray], List[Dict], int]:
        """
        Collect frames from generator while performing streaming person detection.
        
        Optimizations:
        - Processes YOLO in mini-batches during capture
        - Early exit when person detected (skips remaining YOLO)
        - Caches detection results for processed frames only
        
        Returns:
            Tuple of (has_person, collected_frames, cached_detections, frames_processed)
        """
        frames = []
        mini_batch = []
        has_person = False
        largest_person_bbox = None
        cached_detections = []  # Store YOLO results for reuse
        frames_processed = 0
        
        try:
            for frame in self._record_frames_streaming():
                frames.append(frame)
                mini_batch.append(frame)
                
                # Process mini-batch when full
                if len(mini_batch) >= self.batch_size:
                    person_detected, bbox, batch_detections = self._detect_person_in_batch(mini_batch)
                    
                    # Cache detection results for overlay phase
                    cached_detections.extend(batch_detections)
                    frames_processed += len(mini_batch)
                    
                    if person_detected:
                        has_person = True
                        if bbox is not None:
                            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                            if largest_person_bbox is None or area > largest_person_bbox[4]:
                                largest_person_bbox = np.append(bbox, area)
                    
                    # Cleanup mini-batch
                    del mini_batch
                    mini_batch = []
                    
                    # Early exit optimization: stop YOLO after person found
                    if has_person:
                        self.logger.debug(f"Person detected, early exit after {frames_processed} frames")
                        # Collect remaining frames WITHOUT running YOLO
                        for remaining_frame in self._record_frames_streaming():
                            frames.append(remaining_frame)
                        break
            
            # Process any remaining frames in mini-batch
            if mini_batch:
                person_detected, bbox, batch_detections = self._detect_person_in_batch(mini_batch)
                cached_detections.extend(batch_detections)
                frames_processed += len(mini_batch)
                
                if person_detected:
                    has_person = True
                    if bbox is not None:
                        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                        if largest_person_bbox is None or area > largest_person_bbox[4]:
                            largest_person_bbox = np.append(bbox, area)
                del mini_batch
            
            # Apply anti-spam detection
            if has_person and largest_person_bbox is not None:
                person_bbox = largest_person_bbox[:4]
                if self._should_suppress_person_detection(person_bbox):
                    self.logger.debug("Person detection suppressed - anti-spam triggered")
                    has_person = False
                else:
                    self._update_person_tracking(person_bbox)
                del largest_person_bbox
            
            self.logger.debug(f"Collected {len(frames)} frames, processed {frames_processed} with YOLO, person={has_person}")
            return has_person, frames, cached_detections, frames_processed
            
        except Exception as e:
            self.logger.error(f"Frame collection error: {e}")
            return False, frames, [], 0
        finally:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    
    def _detect_person_in_batch(self, batch: List[np.ndarray]) -> Tuple[bool, Optional[np.ndarray], List[Dict]]:
        """
        Detect person in a batch of frames, returning largest bounding box and cached detections.
        
        Args:
            batch: List of frames to process
            
        Returns:
            Tuple of (person_detected, largest_bbox, cached_detections)
            cached_detections: List of dicts with 'boxes' and 'classes' for each frame
        """
        if not batch:
            return False, None, []
        
        largest_bbox = None
        person_found = False
        cached_detections = []
        
        try:
            results = self.model.predict(batch, conf=self.confidence, verbose=False)
            
            for result in results:
                # Cache detection results for this frame
                frame_detection = {'boxes': None, 'classes': None}
                
                if result.boxes is not None and len(result.boxes) > 0:
                    classes = result.boxes.cls.cpu().numpy()
                    boxes = result.boxes.xyxy.cpu().numpy()
                    
                    # Store copies for caching
                    frame_detection['boxes'] = boxes.copy()
                    frame_detection['classes'] = classes.copy()
                    
                    for box, cls_id in zip(boxes, classes):
                        if int(cls_id) == 0:  # Person class
                            person_found = True
                            area = (box[2] - box[0]) * (box[3] - box[1])
                            if largest_bbox is None or area > (largest_bbox[2] - largest_bbox[0]) * (largest_bbox[3] - largest_bbox[1]):
                                largest_bbox = box.copy()
                    
                    del classes, boxes
                
                cached_detections.append(frame_detection)
                del result
            
            del results
            
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            
            return person_found, largest_bbox, cached_detections
            
        except Exception as e:
            self.logger.error(f"Person detection error: {e}")
            return False, None, []
    
    def _apply_frame_overlays(
        self, 
        frames: List[np.ndarray], 
        has_person: bool, 
        cached_detections: Optional[List[Dict]] = None,
        frames_processed: int = 0
    ) -> List[np.ndarray]:
        """
        Apply appropriate overlays to frames based on detection result.
        
        Args:
            frames: Raw frames to process
            has_person: Whether person was detected
            cached_detections: Pre-computed YOLO results for early frames
            frames_processed: Number of frames with cached detections
            
        Returns:
            List of frames with overlays applied
        """
        processed_frames = []
        
        try:
            if has_person:
                # Part 1: Use cached detections for frames that were processed
                if cached_detections and len(cached_detections) > 0:
                    cached_count = len(cached_detections)
                    self.logger.debug(f"Using {cached_count} cached detections, processing {len(frames) - cached_count} remaining frames")
                    
                    for frame, detection in zip(frames[:cached_count], cached_detections):
                        frame_copy = frame.copy()
                        
                        if detection['boxes'] is not None and detection['classes'] is not None:
                            boxes = detection['boxes']
                            classes = detection['classes']
                            
                            for box, cls_id in zip(boxes, classes):
                                if int(cls_id) == 0:  # Person class
                                    x1, y1, x2, y2 = box.astype(int)
                                    cv2.rectangle(frame_copy, (x1, y1), (x2, y2), (0, 0, 255), 3)
                        
                        processed_frames.append(frame_copy)
                    
                    # Cleanup cached detections
                    for detection in cached_detections:
                        if detection['boxes'] is not None:
                            del detection['boxes']
                        if detection['classes'] is not None:
                            del detection['classes']
                    del cached_detections
                
                # Part 2: Process remaining frames (after early exit)
                remaining_frames = frames[len(processed_frames):]
                if remaining_frames:
                    self.logger.debug(f"Processing {len(remaining_frames)} remaining frames with YOLO")
                    for i in range(0, len(remaining_frames), self.batch_size):
                        batch = remaining_frames[i:i + self.batch_size]
                        results = self.model.predict(batch, conf=self.confidence, verbose=False)
                        
                        for j, result in enumerate(results):
                            frame_copy = batch[j].copy()
                            
                            if result.boxes is not None and len(result.boxes) > 0:
                                boxes = result.boxes.xyxy.cpu().numpy()
                                classes = result.boxes.cls.cpu().numpy()
                                
                                for box, cls_id in zip(boxes, classes):
                                    if int(cls_id) == 0:  # Person class
                                        x1, y1, x2, y2 = box.astype(int)
                                        cv2.rectangle(frame_copy, (x1, y1), (x2, y2), (0, 0, 255), 3)
                                
                                del boxes, classes
                            
                            processed_frames.append(frame_copy)
                            del result
                        
                        del results, batch
                        
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
            else:
                # Apply motion detection overlay
                for frame in frames:
                    frame_copy = frame.copy()
                    fg_mask = self.bg_subtractor.apply(frame_copy.copy())
                    motion_pixels = cv2.countNonZero(fg_mask)
                    total_pixels = fg_mask.shape[0] * fg_mask.shape[1]
                    motion_percent = int((motion_pixels / total_pixels) * 100)
                    self._add_motion_overlay(frame_copy, fg_mask, motion_percent)
                    processed_frames.append(frame_copy)
                    del fg_mask, frame_copy
            
            return processed_frames
            
        except Exception as e:
            self.logger.error(f"Overlay application error: {e}")
            return frames  # Return raw frames on error
        finally:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    
    def _generate_gif(
        self, 
        frames: List[np.ndarray], 
        output_path: Path, 
        fps: int
    ) -> bool:
        """
        Generate optimized GIF with smart temporal sampling.
        
        Args:
            frames: Input frames
            output_path: Output GIF file path
            fps: Target frame rate
            
        Returns:
            bool: True if GIF created successfully
        """
        try:
            if not frames:
                self.logger.warning("No frames provided for GIF generation")
                return False
            
            # Smart frame selection
            selected_frames = self._select_key_frames(frames)
            
            # Convert and optimize
            gif_frames = []
            for frame in selected_frames:
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb_frame = self._resize_for_gif(rgb_frame)
                gif_frames.append(rgb_frame)
            
            # Generate GIF
            imageio.mimsave(
                str(output_path),
                gif_frames,
                format='GIF',
                fps=fps,
                loop=0,
                quantizer='nq'
            )
            
            if output_path.exists():
                file_size = output_path.stat().st_size / 1024
                self.logger.info(f"GIF created: {output_path.name} ({file_size:.1f}KB, {len(gif_frames)} frames)")
                return True
            
            return False
            
        except (OSError, IOError) as e:
            self.logger.error(f"File I/O error during GIF generation: {e}")
            return False
        except (ValueError, RuntimeError) as e:
            self.logger.error(f"GIF generation error: {e}")
            return False

    def _select_key_frames(self, frames: List[np.ndarray]) -> List[np.ndarray]:
        """
        Select key frames using weighted temporal distribution.
        
        Emphasizes beginning (trigger moment) and distributes remaining
        frames evenly for smooth motion representation.
        """
        if len(frames) <= self.max_gif_frames:
            return frames
        
        n_frames = len(frames)
        selected_indices = []
        
        # Take more frames from beginning (40% of budget for first third)
        beginning_budget = int(self.max_gif_frames * 0.4)
        beginning_end = n_frames // 3
        beginning_step = max(1, beginning_end / beginning_budget)
        
        for i in range(beginning_budget):
            idx = min(int(i * beginning_step), beginning_end - 1)
            selected_indices.append(idx)
        
        # Distribute remaining frames evenly across the rest
        remaining_budget = self.max_gif_frames - len(selected_indices)
        remaining_start = beginning_end
        remaining_span = n_frames - remaining_start
        remaining_step = remaining_span / remaining_budget
        
        for i in range(remaining_budget):
            idx = remaining_start + int(i * remaining_step)
            selected_indices.append(min(idx, n_frames - 1))
        
        # Remove duplicates and sort
        selected_indices = sorted(set(selected_indices))
        
        # Ensure we have the last frame for closure
        if selected_indices[-1] != n_frames - 1:
            selected_indices[-1] = n_frames - 1
        
        return [frames[i] for i in selected_indices]

    def _resize_for_gif(self, frame: np.ndarray) -> np.ndarray:
        """Resize frame with max side 640px, preserving aspect ratio and even dimensions."""
        height, width = frame.shape[:2]
        max_dimension = 640
        
        # Skip if already within bounds
        if max(height, width) <= max_dimension:
            return frame
        
        # Calculate scale factor based on largest dimension
        scale = max_dimension / max(height, width)
        new_width = int(width * scale)
        new_height = int(height * scale)
        
        # Ensure even dimensions for optimal compression
        new_width += new_width % 2
        new_height += new_height % 2
        
        return cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
    
    def _cleanup_completed_gifs(self) -> None:
        """Remove completed GIF generation tasks from pending list."""
        self._pending_gifs = [f for f in self._pending_gifs if not f.done()]
    
    def _wait_for_pending_gifs(self, timeout: float = 10.0) -> None:
        """Wait for all pending GIF operations to complete."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
            
        if not self._pending_gifs:
            return
            
        self.logger.info(f"Waiting for {len(self._pending_gifs)} pending GIF operations...")

        for future in self._pending_gifs:
            try:
                future.result(timeout=timeout)
            except Exception as e:
                self.logger.warning(f"GIF operation failed: {e}")
        
        self._pending_gifs.clear()
    
    @property
    def status(self) -> Dict[str, Any]:
        """
        Get current system status.
        
        Returns:
            dict: System status information
        """
        return {
            'active': self.is_active,
            'capture_connected': self.capture.is_connected,
            'pending_gifs': len(self._pending_gifs),
            'batch_size': self.batch_size,
            'gpu_available': torch.cuda.is_available(),
            'model_loaded': hasattr(self, 'model'),
            'person_cooldown_active': time.time() - self.last_person_detection < self.person_cooldown_seconds,
            'last_person_detection': self.last_person_detection,
        }
            
    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        self.stop()
    
    def __repr__(self) -> str:
        """String representation for debugging."""
        status = "active" if self.is_active else "inactive"
        return f"SecuritySystem(status={status}, capture={self.capture})"