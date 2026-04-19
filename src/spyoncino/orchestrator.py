"""
Orchestrator - Main loop for collecting metrics and events.

Coordinates inputs, preprocessing, inference, and interfaces.
Collects general metrics and events for permanent storage.
"""

import time
import os
import sys
import logging
import threading
import inspect
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from pathlib import Path

from .input.cam_grabber import CamGrabber
from .preproc.motion_detection import MotionDetection
from .inference.object_detection import ObjectDetection
from .interface.memory_manager import MemoryManager, EventType
from .media_store import MediaStore
from .recipe_classes import resolve_recipe_class
from .recipe_paths import (
    resolve_data_root,
    resolve_inference_weights,
    resolve_path_for_recipe,
    resolve_secrets_path,
    sqlite_path_from_recipe,
)
from .runtime import SpyoncinoRuntime


class Orchestrator:
    """
    Main orchestrator loop that coordinates all components.

    Responsibilities:
    - Collect metrics (uptime, service status, events)
    - Process inputs (cameras)
    - Run preprocessing (motion detection)
    - Run inference (object detection)
    - Send results to interfaces
    """

    def __init__(
        self, recipe: Dict[str, Any], memory_manager: Optional[MemoryManager] = None
    ):
        """
        Initialize the orchestrator.

        Args:
            recipe: Configuration recipe dictionary
            memory_manager: Optional MemoryManager instance (creates default if None)
        """
        self.recipe = recipe
        self.data_root: Optional[Path] = resolve_data_root(recipe)
        self.running = False
        self._control_lock = threading.RLock()
        self._paused = False
        self.media_store: Optional[MediaStore] = None
        self.runtime: Optional[SpyoncinoRuntime] = None
        self._retention_every_n_cycles = 120
        self._media_retention_days: Optional[int] = None
        self._media_max_total_mb: Optional[float] = None
        self._media_max_files_per_camera: Optional[int] = None
        self._event_log_retention_days = 3
        self._event_retention_every_n_cycles = 120
        self._restart_delay_seconds = int(recipe.get("restart_delay_seconds", 45) or 45)
        self._restart_scheduled_at: Optional[datetime] = None
        self._restart_reason: Optional[str] = None

        # Logging
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info("Initializing MemoryManager...")
        if memory_manager is not None:
            self.memory_manager = memory_manager
            self.logger.info(
                "MemoryManager: using injected instance (%s)", memory_manager.db_path
            )
        else:
            db_path = str(sqlite_path_from_recipe(recipe))
            self.memory_manager = MemoryManager(db_path=db_path)
            self.logger.info("MemoryManager initialized (db=%s)", db_path)

        # Component lists
        self.inputs: List[CamGrabber] = []
        self.preproc: List[MotionDetection] = []
        self.inference: List[ObjectDetection] = []
        self.postproc: List[Any] = []
        self.interfaces: List[Any] = []

        # Configuration
        self.patrol_time = recipe.get("patrol_time", 1.0)
        self.secrets_path = resolve_secrets_path(recipe)

        # Metrics tracking
        self.start_time = datetime.now()
        self.total_cycles = 0
        self.total_events = 0

        # Log startup
        self.memory_manager.log_event(
            EventType.STARTUP, "Orchestrator initialized", severity="info"
        )

    def _sync_patrol_time_from_db(self) -> None:
        """Apply optional ``patrol_time`` from SQLite (``/config`` / dashboard); else recipe default."""
        recipe_pt = float(self.recipe.get("patrol_time", 1.0) or 1.0)
        db_v = self.memory_manager.get_config("patrol_time", None)
        if db_v is None:
            self.patrol_time = recipe_pt
            return
        try:
            pt = float(db_v)
            self.patrol_time = max(0.2, min(3600.0, pt))
        except (TypeError, ValueError):
            self.patrol_time = recipe_pt

    def schedule_restart_if_needed(self, reason: str) -> Dict[str, Any]:
        """Schedule one self-restart if none is pending."""
        with self._control_lock:
            if self._restart_scheduled_at is not None:
                eta = self._restart_scheduled_at
                now = datetime.now(timezone.utc)
                return {
                    "scheduled": True,
                    "newly_scheduled": False,
                    "reason": self._restart_reason,
                    "scheduled_at": eta.isoformat(),
                    "seconds_until_restart": max(0, int((eta - now).total_seconds())),
                }
            now = datetime.now(timezone.utc)
            delay = max(5, int(self._restart_delay_seconds))
            eta = now + timedelta(seconds=delay)
            self._restart_scheduled_at = eta
            self._restart_reason = reason
            self.logger.warning(
                "Restart scheduled in %ss (%s)", delay, self._restart_reason
            )
            return {
                "scheduled": True,
                "newly_scheduled": True,
                "reason": self._restart_reason,
                "scheduled_at": eta.isoformat(),
                "seconds_until_restart": delay,
            }

    def get_restart_schedule_status(self) -> Dict[str, Any]:
        with self._control_lock:
            eta = self._restart_scheduled_at
            reason = self._restart_reason
        if eta is None:
            return {
                "scheduled": False,
                "reason": None,
                "scheduled_at": None,
                "seconds_until_restart": None,
            }
        now = datetime.now(timezone.utc)
        return {
            "scheduled": True,
            "reason": reason,
            "scheduled_at": eta.isoformat(),
            "seconds_until_restart": max(0, int((eta - now).total_seconds())),
        }

    def _maybe_execute_scheduled_restart(self) -> None:
        with self._control_lock:
            eta = self._restart_scheduled_at
            reason = self._restart_reason
        if eta is None:
            return
        if datetime.now(timezone.utc) < eta:
            return
        self.logger.warning("Executing scheduled self-restart (%s)", reason)
        try:
            self.memory_manager.log_event(
                EventType.SHUTDOWN,
                f"Scheduled restart triggered ({reason})",
                severity="warning",
            )
        except Exception:
            self.logger.debug("failed to log restart event", exc_info=True)
        try:
            # Intentional process replacement for scheduled self-restart (no shell).
            os.execv(sys.executable, [sys.executable, *sys.argv])  # nosec B606
        except Exception as e:
            self.logger.error("Self-restart failed: %s", e, exc_info=True)

    def build(self) -> None:
        """Build all components from recipe."""
        try:
            self.logger.info("Building inputs...")
            # Build inputs
            for input_config in self.recipe.get("inputs", []):
                self.logger.info(
                    f"Building input: {input_config.get('name', 'unknown')}"
                )
                input_class = self._get_class(input_config["class"])
                input_instance = input_class(**input_config.get("params", {}))
                self.inputs.append(input_instance)
                self.logger.info(f"Built input: {input_config.get('name', 'unknown')}")

            self.logger.info("Building preprocessing...")
            # Build preprocessing
            for preproc_config in self.recipe.get("preproc", []):
                self.logger.info(
                    f"Building preprocessor: {preproc_config.get('name', 'unknown')}"
                )
                preproc_class = self._get_class(preproc_config["class"])
                preproc_instance = preproc_class(**preproc_config.get("params", {}))
                self.preproc.append(preproc_instance)
                self.logger.info(
                    f"Built preprocessor: {preproc_config.get('name', 'unknown')}"
                )

            self.logger.info("Building inference...")
            # Build inference
            for inference_config in self.recipe.get("inference", []):
                self.logger.info(
                    f"Building inference: {inference_config.get('name', 'unknown')}"
                )
                inference_class = self._get_class(inference_config["class"])
                inf_params = dict(inference_config.get("params", {}))
                w = inf_params.get("weights")
                if isinstance(w, str) and w.strip():
                    inf_params["weights"] = resolve_inference_weights(
                        self.recipe, w.strip()
                    )
                inference_instance = inference_class(**inf_params)
                self.inference.append(inference_instance)
                self.logger.info(
                    f"Built inference: {inference_config.get('name', 'unknown')}"
                )

            self.logger.info("Building post-processing...")
            for post_config in self.recipe.get("postproc", []):
                self.logger.info(
                    f"Building post-processor: {post_config.get('name', 'unknown')}"
                )
                post_class = self._get_class(post_config["class"])
                params = dict(post_config.get("params", {}))
                try:
                    resolved_post = resolve_recipe_class(
                        str(post_config.get("class") or "")
                    )
                except ValueError:
                    resolved_post = ""
                if resolved_post.endswith(".FaceIdentification"):
                    gp = params.get("gallery_path")
                    if isinstance(gp, str) and gp.strip():
                        params["gallery_path"] = str(
                            resolve_path_for_recipe(self.recipe, gp.strip())
                        )
                try:
                    post_sig = inspect.signature(post_class.__init__)
                    post_supported = set(post_sig.parameters.keys())
                except (TypeError, ValueError):
                    post_supported = set()
                if (
                    "memory_manager" not in params
                    and "memory_manager" in post_supported
                ):
                    params["memory_manager"] = self.memory_manager
                post_instance = post_class(**params)
                self.postproc.append(post_instance)
                self.logger.info(
                    f"Built post-processor: {post_config.get('name', 'unknown')}"
                )

            media_cfg = self.recipe.get("media") or {}
            media_root = media_cfg.get("root", "media")
            root_path = resolve_path_for_recipe(self.recipe, str(media_root))
            self.media_store = MediaStore(root_path)
            self.media_store.ensure_root()
            self._retention_every_n_cycles = int(
                media_cfg.get("retention_every_n_cycles", 120)
            )
            self._media_retention_days = media_cfg.get("retention_days")
            self._media_max_total_mb = media_cfg.get("max_total_mb")
            self._media_max_files_per_camera = media_cfg.get("max_files_per_camera")
            self.runtime = SpyoncinoRuntime(self, self.media_store)
            self.logger.info(
                "Media store at %s (retention_days=%s, max_total_mb=%s, max_files_per_camera=%s)",
                root_path,
                self._media_retention_days,
                self._media_max_total_mb,
                self._media_max_files_per_camera,
            )

            el_cfg = self.recipe.get("event_log") or {}
            if not isinstance(el_cfg, dict):
                el_cfg = {}
            self._event_log_retention_days = int(el_cfg.get("retention_days", 3))
            self._event_retention_every_n_cycles = int(
                el_cfg.get(
                    "retention_every_n_cycles",
                    media_cfg.get("retention_every_n_cycles", 120),
                )
            )
            self.logger.info(
                "Event log DB retention: retention_days=%s, every_n_cycles=%s",
                self._event_log_retention_days,
                self._event_retention_every_n_cycles,
            )

            self.logger.info("Building interfaces...")
            # Build interfaces
            for interface_config in self.recipe.get("interfaces", []):
                self.logger.info(
                    f"Building interface: {interface_config.get('name', 'unknown')}"
                )
                interface_class = self._get_class(interface_config["class"])
                # Pass memory_manager to interfaces that need it
                params = dict(interface_config.get("params", {}))
                try:
                    init_sig = inspect.signature(interface_class.__init__)
                    supported_params = set(init_sig.parameters.keys())
                except (TypeError, ValueError):
                    init_sig = None
                    supported_params = set()
                if (
                    "memory_manager" not in params
                    or params.get("memory_manager") is None
                ):
                    params["memory_manager"] = self.memory_manager
                if params.get("runtime") is None and self.runtime is not None:
                    if not supported_params or "runtime" in supported_params:
                        params["runtime"] = self.runtime
                if params.get("media_store") is None and self.media_store is not None:
                    if not supported_params or "media_store" in supported_params:
                        params["media_store"] = self.media_store
                if self.secrets_path and params.get("secrets_path") is None:
                    if not supported_params or "secrets_path" in supported_params:
                        params["secrets_path"] = self.secrets_path
                interface_instance = interface_class(**params)
                self.interfaces.append(interface_instance)
                self.logger.info(
                    f"Built interface: {interface_config.get('name', 'unknown')}"
                )

                # Start interface services that need background threads
                if hasattr(interface_instance, "run"):
                    # Check if it's a webapp (has host and port attributes)
                    if hasattr(interface_instance, "host") and hasattr(
                        interface_instance, "port"
                    ):
                        self.logger.info(
                            f"Starting webapp server for {interface_config.get('name', 'unknown')}..."
                        )
                        server_thread = threading.Thread(
                            target=interface_instance.run,
                            daemon=True,
                            name=f"WebApp-{interface_config.get('name', 'unknown')}",
                        )
                        server_thread.start()
                        self.logger.info(
                            f"Webapp server started in background thread on {interface_instance.host}:{interface_instance.port}"
                        )

                    # Check if it's a Telegram bot (has app with updater attribute)
                    elif hasattr(interface_instance, "app") and hasattr(
                        interface_instance.app, "updater"
                    ):
                        self.logger.info(
                            f"Starting Telegram bot for {interface_config.get('name', 'unknown')}..."
                        )
                        bot_thread = threading.Thread(
                            target=interface_instance.run,
                            daemon=True,
                            name=f"TelegramBot-{interface_config.get('name', 'unknown')}",
                        )
                        bot_thread.start()
                        self.logger.info("Telegram bot started in background thread")

            self.logger.info("All components built successfully")

        except Exception as e:
            self.logger.error(f"Failed to build components: {e}", exc_info=True)
            raise

    def _get_class(self, class_path: str):
        """
        Get class from a recipe ``class`` entry (alias or dotted import path).

        Args:
            class_path: Short alias (e.g. ``camera``) or dotted path

        Returns:
            Class object
        """
        resolved = resolve_recipe_class(class_path)
        parts = resolved.split(".")
        module_path = ".".join(parts[:-1])
        class_name = parts[-1]

        module = __import__(module_path, fromlist=[class_name])
        return getattr(module, class_name)

    def _update_service_status(self) -> None:
        """Update service status in memory manager."""
        uptime = (datetime.now() - self.start_time).total_seconds()

        # Update orchestrator status
        self.memory_manager.update_service_status(
            "orchestrator", is_running=self.running, uptime_seconds=uptime
        )

        # Update input statuses
        for input_cam in self.inputs:
            self.memory_manager.update_service_status(
                f"input_{input_cam.cam_id}",
                is_running=input_cam.running
                if hasattr(input_cam, "running")
                else False,
                last_error=None
                if (hasattr(input_cam, "connected") and input_cam.connected)
                else "Camera disconnected",
            )

    def _process_input(self, input_cam: CamGrabber) -> Optional[Dict[str, Any]]:
        """
        Process a single input camera.

        Args:
            input_cam: Camera grabber instance

        Returns:
            Result dictionary or None if processing failed
        """
        try:
            camera_id = input_cam.cam_id

            # -------------------------------------------------------------------------
            # Procedure: capture, preprocess, infer
            # -------------------------------------------------------------------------
            # Get snap and record
            self.logger.info(f"Getting snap from camera {camera_id}")
            snap = input_cam.snap()
            self.logger.info(f"Got snap, getting record from camera {camera_id}")
            record = input_cam.record()
            self.logger.info(f"Got record from camera {camera_id}")

            if snap is None:
                self.logger.warning(f"Snap is None for camera {camera_id}, skipping")
                return None
            if record is None:
                self.logger.warning(f"Record is None for camera {camera_id}, skipping")
                return None
            if len(record) == 0:
                self.logger.warning(f"Record is empty for camera {camera_id}, skipping")
                return None

            # Extract frames
            snap_frame = snap["frame"]
            record_frames = [r["frame"] for r in record]

            # Run motion detection on snap (peak detection)
            motion_detector = self.preproc[0] if self.preproc else None
            object_detector = self.inference[0] if self.inference else None
            face_identifier = self.postproc[0] if self.postproc else None

            is_peak = False
            peak_result = None
            frames_with_labels = []
            object_detected = False
            frames_with_motion = []
            motion_detected = False
            face_identified = False
            face_result = None

            if motion_detector:
                self.logger.info(f"Running motion peak detection on camera {camera_id}")
                is_peak, motion_percent, fg_mask = motion_detector.peak(
                    camera_id, snap_frame
                )
                peak_result = {"motion_percent": motion_percent, "fg_mask": fg_mask}
                self.logger.info(f"Motion peak detection completed: is_peak={is_peak}")

                if is_peak:
                    # Motion detected, run object detection
                    if object_detector:
                        self.logger.info(
                            f"Running object detection on camera {camera_id}"
                        )
                        frames_with_labels, object_detected = object_detector.detect(
                            record_frames
                        )
                        self.logger.info(
                            f"Object detection completed: detected={object_detected}"
                        )

                    # If no object detected, run motion detection on record
                    if not object_detected and motion_detector:
                        self.logger.info(
                            f"Running motion detection on record frames for camera {camera_id}"
                        )
                        frames_with_motion, motion_detected = motion_detector.detect(
                            camera_id, record_frames
                        )
                        self.logger.info(
                            f"Motion detection on record completed: detected={motion_detected}"
                        )
                else:
                    # No peak motion, but check record frames for motion
                    if motion_detector:
                        self.logger.info(
                            f"Running motion detection on record frames for camera {camera_id}"
                        )
                        frames_with_motion, motion_detected = motion_detector.detect(
                            camera_id, record_frames
                        )
                        self.logger.info(
                            f"Motion detection on record completed: detected={motion_detected}"
                        )
            else:
                # No motion detector, just run object detection
                if object_detector:
                    self.logger.info(f"Running object detection on camera {camera_id}")
                    frames_with_labels, object_detected = object_detector.detect(
                        record_frames
                    )
                    self.logger.info(
                        f"Object detection completed: detected={object_detected}"
                    )

            # Face post-processing needs YOLO boxes on the record. OD only ran above on a motion *peak*;
            # if the buffer shows motion without a peak, run OD once so face_identification can still run.
            if (
                motion_detector
                and object_detector
                and face_identifier
                and bool(getattr(face_identifier, "enabled", False))
                and not object_detected
                and motion_detected
            ):
                self.logger.info(
                    "Record motion without prior person alarm — running object detection for face pipeline "
                    "on camera %s",
                    camera_id,
                )
                frames_with_labels, object_detected = object_detector.detect(
                    record_frames
                )
                self.logger.info(
                    "Object detection (for face) completed: detected=%s",
                    object_detected,
                )

            if face_identifier:
                if object_detected:
                    self.logger.info(
                        f"Running face identification on camera {camera_id}"
                    )
                    identify_kw: Dict[str, Any] = {
                        "camera_id": camera_id,
                        "memory_manager": self.memory_manager,
                        "media_store": self.media_store,
                    }
                    try:
                        sig = inspect.signature(face_identifier.identify)
                        supported = set(sig.parameters.keys())
                        has_varkw = any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            for p in sig.parameters.values()
                        )
                    except (TypeError, ValueError):
                        supported = set()
                        has_varkw = False
                    if has_varkw:
                        call_kw = dict(identify_kw)
                    else:
                        call_kw = {
                            k: v for k, v in identify_kw.items() if k in supported
                        }
                    face_identified, face_result = face_identifier.identify(
                        record_frames,
                        frames_with_labels,
                        **call_kw,
                    )
                    self.logger.info(
                        f"Face identification completed: identified={face_identified}"
                    )
                else:
                    self.logger.info(
                        f"No object detected, skipping face identification on camera {camera_id}"
                    )

            peak_payload: Dict[str, Any] = {
                "alarmed": is_peak if motion_detector else False,
                "data": peak_result if motion_detector else None,
            }
            detection_payload: Dict[str, Any] = {
                "alarmed": object_detected,
                "data": frames_with_labels,
            }
            motion_payload: Dict[str, Any] = {
                "alarmed": motion_detected,
                "data": frames_with_motion,
            }
            face_payload: Dict[str, Any] = {
                "alarmed": face_identified,
                "data": face_result,
            }

            # -------------------------------------------------------------------------
            # Result construction
            # -------------------------------------------------------------------------
            result: Dict[str, Any] = {
                "camera_id": camera_id,
                "timestamp": snap["timestamp"],
                "snap": snap_frame,
                "record": record_frames,
                "peak": peak_payload,
                "detection": detection_payload,
                "motion": motion_payload,
                "face": face_payload,
            }

            # -------------------------------------------------------------------------
            # Event logging
            # -------------------------------------------------------------------------
            if face_identified:
                face_meta: Dict[str, Any] = {"camera_id": camera_id}
                if isinstance(face_result, dict):
                    faces = face_result.get("faces") or []
                    hints = [
                        f.get("notification_hint") for f in faces if isinstance(f, dict)
                    ]
                    face_meta["hints"] = hints
                    face_meta["champion_frame_index"] = face_result.get(
                        "champion_frame_index"
                    )
                    known_names: List[str] = []
                    unknown_n = 0
                    for f in faces:
                        if not isinstance(f, dict):
                            continue
                        if f.get("notification_hint") == "known_text" and f.get(
                            "display_name"
                        ):
                            known_names.append(str(f["display_name"]).strip())
                        elif f.get("notification_hint") == "unknown_prompt":
                            unknown_n += 1
                    if known_names:
                        face_meta["known_display_names"] = list(
                            dict.fromkeys(known_names)
                        )
                    if unknown_n > 0:
                        face_meta["unknown_face_count"] = int(unknown_n)
                self.memory_manager.log_event(
                    EventType.FACE,
                    f"Face alert on camera {camera_id}",
                    metadata=face_meta,
                    severity="info",
                    camera_id=camera_id,
                )
                self.total_events += 1
            elif object_detected:
                self.memory_manager.log_event(
                    EventType.PERSON,
                    f"Person detected on camera {camera_id}",
                    metadata={
                        "camera_id": camera_id,
                        "detection_count": len(frames_with_labels),
                    },
                    severity="warning",
                    camera_id=camera_id,
                )
                self.total_events += 1
            elif motion_detected:
                # Motion detected but no person detected
                self.memory_manager.log_event(
                    EventType.MOTION,
                    f"Motion detected on camera {camera_id}",
                    metadata={"camera_id": camera_id},
                    severity="info",
                    camera_id=camera_id,
                )
                self.total_events += 1
            elif motion_detector:
                # No motion detected and no person detected (only log if we have a motion detector)
                # Log periodically to avoid spam (every 10 cycles)
                if self.total_cycles % 10 == 0:
                    self.memory_manager.log_event(
                        EventType.MOTION,
                        f"No motion or detection on camera {camera_id}",
                        metadata={
                            "camera_id": camera_id,
                            "peak_motion": is_peak if motion_detector else False,
                        },
                        severity="info",
                        camera_id=camera_id,
                    )

            return result

        except Exception as e:
            self.logger.error(
                f"Error processing input {input_cam.cam_id}: {e}", exc_info=True
            )
            self.memory_manager.log_event(
                EventType.ERROR,
                f"Error processing camera {input_cam.cam_id}: {str(e)}",
                severity="error",
                camera_id=input_cam.cam_id,
            )
            return None

    def run(self) -> None:
        """Run the main orchestrator loop."""
        self.running = True
        self.logger.info("Orchestrator started")

        self.memory_manager.log_event(
            EventType.STARTUP, "Orchestrator main loop started", severity="info"
        )

        try:
            self.logger.info(f"Entering main loop (patrol_time={self.patrol_time}s)")
            while self.running:
                self._sync_patrol_time_from_db()
                self._maybe_execute_scheduled_restart()
                cycle_start = time.time()
                if self.total_cycles % 10 == 0:  # Log every 10 cycles
                    self.logger.info(f"Cycle {self.total_cycles + 1} started")

                # Update service status
                self._update_service_status()

                with self._control_lock:
                    paused = self._paused
                if paused:
                    elapsed = time.time() - cycle_start
                    sleep_time = max(0, self.patrol_time - elapsed)
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    self.total_cycles += 1
                    if (
                        self.media_store
                        and self._retention_every_n_cycles > 0
                        and self.total_cycles % self._retention_every_n_cycles == 0
                    ):
                        self._maybe_run_media_retention()
                    if (
                        self._event_retention_every_n_cycles > 0
                        and self.total_cycles % self._event_retention_every_n_cycles
                        == 0
                    ):
                        self._maybe_run_event_log_retention()
                    continue

                # Process all inputs
                for input_cam in self.inputs:
                    self.logger.info(f"Processing input: {input_cam.cam_id}")
                    result = self._process_input(input_cam)
                    self.logger.info(f"Input {input_cam.cam_id} processed")

                    if result:
                        # Send to all interfaces
                        for interface in self.interfaces:
                            try:
                                self.logger.info(
                                    f"Sending result to interface: {interface.__class__.__name__}"
                                )
                                if hasattr(interface, "process"):
                                    interface.process(result)
                                elif hasattr(interface, "handle_event"):
                                    interface.handle_event(result)
                                self.logger.info(
                                    f"Interface {interface.__class__.__name__} processed"
                                )
                            except Exception as e:
                                self.logger.error(
                                    f"Error in interface {interface.__class__.__name__}: {e}",
                                    exc_info=True,
                                )

                self.total_cycles += 1

                if (
                    self.media_store
                    and self._retention_every_n_cycles > 0
                    and self.total_cycles % self._retention_every_n_cycles == 0
                ):
                    self._maybe_run_media_retention()
                if (
                    self._event_retention_every_n_cycles > 0
                    and self.total_cycles % self._event_retention_every_n_cycles == 0
                ):
                    self._maybe_run_event_log_retention()

                # Save metrics snapshot periodically (every 60 cycles)
                if self.total_cycles % 60 == 0:
                    self.logger.info(
                        f"Saving metrics snapshot (cycle {self.total_cycles})"
                    )
                    metrics = self.memory_manager.get_current_metrics()
                    self.memory_manager.save_metrics_snapshot(metrics)

                # Sleep for patrol time
                elapsed = time.time() - cycle_start
                sleep_time = max(0, self.patrol_time - elapsed)
                if sleep_time > 0:
                    if self.total_cycles % 10 == 0:  # Log every 10 cycles
                        self.logger.info(
                            f"Cycle {self.total_cycles} completed in {elapsed:.2f}s, sleeping for {sleep_time:.2f}s"
                        )
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            self.logger.info("Orchestrator interrupted by user")
        except Exception as e:
            self.logger.error(f"Orchestrator error: {e}", exc_info=True)
            self.memory_manager.log_event(
                EventType.ERROR, f"Orchestrator error: {str(e)}", severity="error"
            )
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the orchestrator."""
        self.running = False
        self.logger.info("Orchestrator stopped")

        self.memory_manager.log_event(
            EventType.SHUTDOWN, "Orchestrator stopped", severity="info"
        )

        # Update final status
        self._update_service_status()

    def _maybe_run_media_retention(self) -> None:
        if not self.media_store:
            return
        try:
            stats = self.memory_manager.apply_media_retention(
                self.media_store.root,
                retention_days=self._media_retention_days,
                max_total_mb=self._media_max_total_mb,
                max_files_per_camera=self._media_max_files_per_camera,
            )
            total = sum(stats.values())
            if total > 0:
                self.logger.info("Media retention: %s", stats)
            pruned = self.memory_manager.cleanup_expired_pending_faces(
                self.media_store.root
            )
            if pruned > 0:
                self.logger.info("Expired pending faces cleaned: %s rows", pruned)
        except Exception as e:
            self.logger.error("Media retention error: %s", e, exc_info=True)

    def _maybe_run_event_log_retention(self) -> None:
        """Prune old ``events`` and ``metrics`` rows per recipe ``event_log.retention_days``."""
        if self._event_log_retention_days <= 0:
            return
        try:
            deleted = self.memory_manager.cleanup_old_data(
                days=self._event_log_retention_days
            )
            if deleted > 0:
                self.logger.info("Event log retention: removed %s old DB rows", deleted)
        except Exception as e:
            self.logger.error("Event log retention error: %s", e, exc_info=True)


def main():
    import argparse
    import yaml
    import sys

    argv = sys.argv[1:]
    if argv and argv[0] == "discover":
        from .discovery_app import main as discover_main

        sys.argv = [sys.argv[0]] + argv[1:]
        discover_main()
        return
    if argv and argv[0] == "recipe-builder":
        from .recipe_builder_app import main as recipe_builder_main

        sys.argv = [sys.argv[0]] + argv[1:]
        recipe_builder_main()
        return

    # Setup basic logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    from .logging_redact import install_telegram_token_log_redaction

    install_telegram_token_log_redaction()
    logger = logging.getLogger("main")

    parser = argparse.ArgumentParser(
        description="Run Orchestrator with a recipe YAML file.",
        epilog=(
            "Camera discovery: spyoncino discover | "
            "Standalone recipe builder: spyoncino recipe-builder (default port 8002). "
            "If data/config/recipe.yaml is missing, running spyoncino without a path starts "
            "the builder on --listen-port (default 8000), then continues after **Save & start**."
        ),
    )
    parser.add_argument(
        "recipe_path",
        nargs="?",
        type=str,
        help="Path to recipe YAML file (optional if data/config/recipe.yaml exists)",
    )
    parser.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help=(
            "Bind address for the first-run recipe builder when data/config/recipe.yaml "
            "is missing (default 127.0.0.1)."
        ),
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=8000,
        help=(
            "Port for the first-run recipe builder when no default recipe exists "
            "(default 8000)."
        ),
    )
    args = parser.parse_args()
    default_recipe_path = (Path.cwd() / "data" / "config" / "recipe.yaml").resolve()
    if args.recipe_path:
        resolved_recipe_path = Path(args.recipe_path).expanduser().resolve()
    else:
        resolved_recipe_path = default_recipe_path
        if not resolved_recipe_path.is_file():
            import asyncio

            from .recipe_builder_app import run_bootstrap_until_launch

            logger.info(
                "No default recipe at %s — starting first-run builder on http://%s:%s",
                resolved_recipe_path,
                args.listen_host,
                args.listen_port,
            )
            print(
                f"\nNo recipe at:\n  {resolved_recipe_path}\n\n"
                f"Open the recipe builder: http://{args.listen_host}:{args.listen_port}\n"
                "Configure cameras, then use **Save & start Spyoncino** (same process; Ctrl+C stops everything).\n",
                file=sys.stderr,
            )
            try:
                launched = asyncio.run(
                    run_bootstrap_until_launch(args.listen_host, args.listen_port)
                )
            except KeyboardInterrupt:
                print("\nStopped.", file=sys.stderr)
                sys.exit(130)
            if not launched:
                sys.exit(0)
            if not resolved_recipe_path.is_file():
                logger.error(
                    "Expected default recipe at %s after setup; exiting.",
                    resolved_recipe_path,
                )
                sys.exit(1)

    logger.info(f"Starting orchestrator with recipe: {resolved_recipe_path}")

    try:
        logger.info("Loading recipe file...")
        with open(resolved_recipe_path, "r", encoding="utf-8") as f:
            recipe = yaml.safe_load(f)
        logger.info("Recipe loaded successfully")
    except Exception as e:
        logger.error(
            f"Failed to load recipe from {resolved_recipe_path}: {e}", exc_info=True
        )
        print(
            f"Failed to load recipe from {resolved_recipe_path}: {e}", file=sys.stderr
        )
        sys.exit(1)

    logger.info("Creating orchestrator instance...")
    orchestrator = Orchestrator(recipe)
    logger.info("Orchestrator instance created")

    logger.info("Building components...")
    orchestrator.build()
    logger.info("Components built, starting main loop...")

    try:
        orchestrator.running = True
        orchestrator.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Error running orchestrator: {e}", exc_info=True)
        print(f"Error running orchestrator: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
