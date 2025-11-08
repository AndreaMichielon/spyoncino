import logging
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from spyoncino.analytics import EventLogger, EventType, SecurityEvent
from spyoncino.security import SecuritySystem


@dataclass
class StorageInfo:
    """Storage space information."""

    total_gb: float
    used_gb: float
    free_gb: float
    usage_percent: float


class SecurityEventManager:
    """
    Professional security event management system.

    Manages security system lifecycle, event handling, storage cleanup,
    and provides hooks for external notification systems.
    """

    def __init__(
        self,
        security_system: SecuritySystem,
        event_folder: str = "events",
        check_interval: float = 5.0,
        retention_hours: int = 24,
        low_space_threshold_gb: float = 1.0,
        aggressive_cleanup_hours: int = 12,
        analytics_figure_width: float = 22,
        analytics_figure_height: float = 5.5,
        analytics_intervals: list[int] = None,
    ):
        """
        Initialize the security event manager.

        Args:
            security_system: SecuritySystem instance to manage
            event_folder: Directory to store event recordings
            check_interval: Time between motion checks (seconds)
            retention_hours: How long to keep recordings (hours)
            low_space_threshold_gb: Threshold for aggressive cleanup (GB)
            aggressive_cleanup_hours: Retention time during low space (hours)
        """
        if not isinstance(security_system, SecuritySystem):
            raise TypeError("security_system must be a SecuritySystem instance")
        if check_interval <= 0:
            raise ValueError("check_interval must be positive")
        if retention_hours <= 0:
            raise ValueError("retention_hours must be positive")
        if low_space_threshold_gb < 0:
            raise ValueError("low_space_threshold_gb must be non-negative")
        if aggressive_cleanup_hours <= 0:
            raise ValueError("aggressive_cleanup_hours must be positive")

        # Core components
        self.security_system = security_system
        self.security_system.set_error_callback(self._handle_security_error)
        self.event_folder = Path(event_folder)
        self.check_interval = check_interval
        self.retention_hours = retention_hours
        self.low_space_threshold_gb = low_space_threshold_gb
        self.aggressive_cleanup_hours = aggressive_cleanup_hours

        # Ensure event folder exists
        self.event_folder.mkdir(parents=True, exist_ok=True)

        # Thread management
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.is_running = False

        # Event statistics
        self._stats = {
            "events_processed": 0,
            "person_events": 0,
            "motion_events": 0,
            "files_cleaned": 0,
            "last_cleanup": None,
            "start_time": None,
        }

        # Initialize event logger with analytics configuration
        self.event_logger = EventLogger(
            db_path=self.event_folder / "events.db",
            analytics_figure_width=analytics_figure_width,
            analytics_figure_height=analytics_figure_height,
            analytics_intervals=analytics_intervals,
        )

        # Event handlers (can be overridden by external systems)
        self.on_disconnect: Callable[[], None] = self._default_disconnect_handler
        self.on_motion: Callable[[str | None], None] = self._default_motion_handler
        self.on_person: Callable[[str | None], None] = self._default_person_handler
        self.on_storage_warning: Callable[[StorageInfo], None] = self._default_storage_handler
        self.security_system.set_person_handler(self._handle_person_detected)
        self.security_system.set_motion_handler(self._handle_motion_detected)

        # Logging
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.debug(f"Event manager initialized: {self.event_folder}")

        # Log startup
        self.event_logger.log_event(
            SecurityEvent(
                timestamp=datetime.now(),
                event_type=EventType.STARTUP,
                message="Security Event Manager initialized",
            )
        )

    def _handle_security_error(self, error_message: str) -> None:
        """Handle errors from security system."""
        self.event_logger.log_event(
            SecurityEvent(
                timestamp=datetime.now(),
                event_type=EventType.ERROR,
                message=error_message,
                severity="error",
            )
        )

    def _default_disconnect_handler(self) -> None:
        """Default handler for capture disconnection."""
        self.logger.warning("Capture disconnected, attempting reconnection...")

        self.event_logger.log_event(
            SecurityEvent(
                timestamp=datetime.now(),
                event_type=EventType.DISCONNECT,
                message="Camera capture disconnected",
                severity="warning",
            )
        )

    def _default_motion_handler(self, event_file: str | None) -> None:
        """Default handler for motion events."""
        self._stats["motion_events"] += 1
        self.logger.info(f"Motion detected: {event_file or 'processing...'}")

    def _default_person_handler(self, event_file: str | None) -> None:
        """Default handler for person detection events."""
        self._stats["person_events"] += 1
        self.logger.info(f"Person detected: {event_file or 'processing...'}")

    def _default_storage_handler(self, storage_info: StorageInfo) -> None:
        """Default handler for low storage warnings."""
        self.logger.warning(
            f"Low disk space: {storage_info.free_gb:.1f}GB free "
            f"({storage_info.usage_percent:.1f}% used)"
        )

        self.event_logger.log_event(
            SecurityEvent(
                timestamp=datetime.now(),
                event_type=EventType.STORAGE_WARNING,
                message=f"Low disk space: {storage_info.free_gb:.1f}GB free",
                metadata={
                    "free_gb": storage_info.free_gb,
                    "used_gb": storage_info.used_gb,
                    "usage_percent": storage_info.usage_percent,
                },
                severity="warning",
            )
        )

    def start(self) -> bool:
        """
        Start the security event manager.

        Returns:
            bool: True if started successfully
        """
        if self.is_running:
            self.logger.warning("Event manager already running")
            return True

        # Start the security system
        if not self.security_system.start():
            self.logger.error("Failed to start security system")
            return False

        # Start monitoring
        self.is_running = True
        self._stop_event.clear()
        self._stats["start_time"] = datetime.now()

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="SecurityEventMonitor", daemon=True
        )
        self._monitor_thread.start()

        self.logger.debug("Security event manager started")
        return True

    def stop(self, timeout: float = 10.0) -> None:
        """
        Stop the security event manager.

        Args:
            timeout: Maximum time to wait for clean shutdown
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        if not self.is_running:
            self.logger.info("Event manager already stopped")
            return

        self.logger.info("Stopping security event manager...")

        self.event_logger.log_event(
            SecurityEvent(
                timestamp=datetime.now(),
                event_type=EventType.SHUTDOWN,
                message="Security Event Manager shutdown initiated",
            )
        )

        # Signal stop
        self.is_running = False
        self._stop_event.set()

        # Wait for monitor thread
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=timeout)
            if self._monitor_thread.is_alive():
                self.logger.warning("Monitor thread did not stop cleanly")

        # Stop security system
        self.security_system.stop()

        # Final cleanup
        self._cleanup_old_events()

        self.logger.info("Security event manager stopped")

    def _monitor_loop(self) -> None:
        """
        Main monitoring loop - now simplified since SecuritySystem handles motion detection.
        This loop just monitors connection status and performs periodic maintenance.
        """
        consecutive_failures = 0
        max_failures = 5
        was_disconnected = False

        while self.is_running and not self._stop_event.is_set():
            try:
                # Check security system connection status
                if not self.security_system.capture.is_connected:
                    if not was_disconnected:
                        was_disconnected = True
                    self.on_disconnect()
                    consecutive_failures += 1
                    if consecutive_failures >= max_failures:
                        self.logger.error("Max connection failures reached")
                        break
                    self._stop_event.wait(timeout=self.check_interval * 2)
                    continue

                if was_disconnected:
                    was_disconnected = False
                    self.logger.info("Camera reconnected successfully")
                    self.event_logger.log_event(
                        SecurityEvent(
                            timestamp=datetime.now(),
                            event_type=EventType.RECONNECT,
                            message="Camera reconnected successfully",
                            severity="info",
                        )
                    )

                consecutive_failures = 0

                # Periodic maintenance
                if self._should_cleanup():
                    self._cleanup_old_events()

                # Wait for next maintenance cycle
                self._stop_event.wait(timeout=60)  # Check every minute

            except KeyboardInterrupt:
                self.logger.info("Monitor loop interrupted by user")
                break
            except Exception as e:
                self.logger.error(f"Monitor loop error: {e}", exc_info=True)

                self.event_logger.log_event(
                    SecurityEvent(
                        timestamp=datetime.now(),
                        event_type=EventType.ERROR,
                        message=f"Monitor loop error: {str(e)[:100]}",
                        metadata={"error_type": type(e).__name__},
                        severity="error",
                    )
                )

                consecutive_failures += 1
                if consecutive_failures >= max_failures:
                    self.logger.error("Too many consecutive failures, stopping")
                    break
                self._stop_event.wait(timeout=self.check_interval)

    def _handle_person_detected(self, gif_path: str) -> None:
        """
        Handle person detection events with guaranteed file path.
        Called by SecuritySystem after GIF is created.
        """
        self.logger.info(f"Person detected - GIF: {gif_path}")
        self._stats["person_events"] += 1
        self._stats["events_processed"] += 1

        self.event_logger.log_event(
            SecurityEvent(
                timestamp=datetime.now(),
                event_type=EventType.PERSON,
                message="Person detected",
                metadata={"gif_path": gif_path},
            )
        )

        # File is guaranteed to exist here - trigger external handler
        if callable(self.on_person):
            try:
                self.on_person(gif_path)
            except Exception as e:
                self.logger.error(f"Person handler failed: {e}", exc_info=True)

    def _handle_motion_detected(self, gif_path: str) -> None:
        """
        Handle motion detection events with guaranteed file path.
        Called by SecuritySystem after GIF is created.
        """
        self.logger.info(f"Motion detected - GIF: {gif_path}")
        self._stats["motion_events"] += 1
        self._stats["events_processed"] += 1

        self.event_logger.log_event(
            SecurityEvent(
                timestamp=datetime.now(),
                event_type=EventType.MOTION,
                message="Motion detected",
                metadata={"gif_path": gif_path},
            )
        )

        # File is guaranteed to exist here - trigger external handler
        if callable(self.on_motion):
            try:
                self.on_motion(gif_path)
            except Exception as e:
                self.logger.error(f"Motion handler failed: {e}", exc_info=True)

    def _should_cleanup(self) -> bool:
        """Determine if cleanup should run based on time since last cleanup."""
        last_cleanup = self._stats.get("last_cleanup")
        if last_cleanup is None:
            return True

        # Run cleanup every hour
        return (datetime.now() - last_cleanup).total_seconds() > 3600

    def _cleanup_all_events(self) -> int:
        """
        Delete ALL GIF files in the event folder regardless of age.

        Returns:
            int: Number of files successfully deleted
        """
        files_deleted = 0
        files_failed = []

        try:
            # Get all GIF files
            gif_files = list(self.event_folder.glob("*.gif"))
            total_files = len(gif_files)

            if total_files == 0:
                self.logger.info("No GIF files to delete")
                return 0

            self.logger.info(f"Deleting all {total_files} GIF files...")

            # Delete each file
            for file_path in gif_files:
                try:
                    if file_path.exists():  # Add existence check
                        file_path.unlink()
                        files_deleted += 1
                        self.logger.debug(f"Deleted: {file_path.name}")
                except (OSError, PermissionError) as e:
                    files_failed.append(file_path.name)
                    self.logger.warning(f"Failed to delete {file_path.name}: {e}")

            # Update statistics
            self._stats["files_cleaned"] += files_deleted

            # Log results
            self.logger.info(f"Cleanup complete: {files_deleted}/{total_files} files deleted")

            if files_failed:
                self.logger.error(
                    f"Failed to delete {len(files_failed)} files: {', '.join(files_failed[:5])}"
                )

            return files_deleted

        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}", exc_info=True)
            return files_deleted

    def _cleanup_old_events(self) -> None:
        """Clean up old event files and manage disk space."""
        try:
            # Get storage information
            storage_info = self.get_storage_info()

            # Determine cleanup policy
            if storage_info.free_gb < self.low_space_threshold_gb:
                # Aggressive cleanup for low disk space
                cutoff_hours = self.aggressive_cleanup_hours
                self.on_storage_warning(storage_info)
            else:
                # Normal cleanup
                cutoff_hours = self.retention_hours

            # Calculate cutoff time
            cutoff_time = datetime.now() - timedelta(hours=cutoff_hours)

            # Clean up old files
            files_deleted = 0
            for file_path in self.event_folder.glob("*.gif"):
                try:
                    if file_path.exists() and self._get_file_timestamp(file_path) < cutoff_time:
                        file_path.unlink()
                        files_deleted += 1
                        self.logger.debug(f"Deleted old file: {file_path.name}")
                except (OSError, PermissionError) as e:
                    self.logger.warning(f"Failed to delete {file_path.name}: {e}")

            # Update statistics
            self._stats["files_cleaned"] += files_deleted
            self._stats["last_cleanup"] = datetime.now()

            if files_deleted > 0:
                self.logger.info(f"Cleanup completed: {files_deleted} files deleted")

        except (OSError, PermissionError) as e:
            self.logger.error(f"Cleanup error: {e}", exc_info=True)

    def _get_file_timestamp(self, file_path: Path) -> datetime:
        """
        Extract timestamp from event filename.

        Args:
            file_path: Path to event file

        Returns:
            datetime: File timestamp or file modification time as fallback
        """
        if not isinstance(file_path, Path):
            raise TypeError("file_path must be a Path object")

        try:
            # Parse filename: event_type_YYYYMMDD_HHMMSS.gif
            parts = file_path.stem.split("_")
            if len(parts) >= 3:
                timestamp_str = parts[1] + parts[2]  # Combine date and time
                return datetime.strptime(timestamp_str, "%Y%m%d%H%M%S")
        except (ValueError, IndexError):
            pass

        # Fallback to file modification time
        try:
            return datetime.fromtimestamp(file_path.stat().st_mtime)
        except (OSError, PermissionError) as e:
            self.logger.warning(f"Cannot get timestamp for {file_path.name}: {e}")
            return datetime.now()

    def get_storage_info(self) -> StorageInfo:
        """
        Get current storage information.

        Returns:
            StorageInfo: Current disk usage statistics
        """
        try:
            total, used, free = shutil.disk_usage(self.event_folder)
            total_gb = total / (1024**3)
            used_gb = used / (1024**3)
            free_gb = free / (1024**3)
            usage_percent = (used / total) * 100

            return StorageInfo(
                total_gb=total_gb, used_gb=used_gb, free_gb=free_gb, usage_percent=usage_percent
            )

        except (OSError, PermissionError) as e:
            self.logger.error(f"Error getting storage info: {e}")
            return StorageInfo(0, 0, 0, 0)

    def list_recordings(self, limit: int | None = None) -> list[str]:
        """
        List available event recordings, most recent first.

        Args:
            limit: Maximum number of recordings to return

        Returns:
            List of file paths sorted by timestamp (newest first)
        """
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")

        try:
            recordings = []
            for file_path in self.event_folder.glob("*.gif"):
                recordings.append((self._get_file_timestamp(file_path), str(file_path)))

            # Sort by timestamp (newest first) and return paths
            sorted_recordings = sorted(recordings, key=lambda x: x[0], reverse=True)
            paths = [path for _, path in sorted_recordings]

            return paths[:limit] if limit else paths

        except (OSError, PermissionError) as e:
            self.logger.error(f"Error listing recordings: {e}")
            return []

    def get_recording(self, event_name: str) -> str | None:
        """
        Get path to specific recording by name.

        Args:
            event_name: Name of event (without .gif extension)

        Returns:
            File path if exists, None otherwise
        """
        if not event_name or not event_name.replace("_", "").replace("-", "").isalnum():
            return None
        if len(event_name) > 50:  # Reasonable filename length limit
            return None

        file_path = self.event_folder / f"{event_name}.gif"
        return str(file_path) if file_path.exists() else None

    def get_recordings_by_type(self, event_type: str) -> list[str]:
        """
        Get recordings filtered by event type.

        Args:
            event_type: 'person' or 'motion'

        Returns:
            List of matching file paths
        """
        valid_types = {"person", "motion"}
        if event_type not in valid_types:
            raise ValueError(f"event_type must be one of {valid_types}")

        pattern = f"{event_type}_*.gif"
        return sorted([str(f) for f in self.event_folder.glob(pattern)], reverse=True)

    def get_statistics(self) -> dict[str, Any]:
        """
        Get current system statistics.

        Returns:
            Dictionary with system statistics and status
        """
        try:
            uptime = None
            if self._stats["start_time"]:
                uptime = datetime.now() - self._stats["start_time"]

            storage = self.get_storage_info()

            return {
                "running": self.is_running,
                "uptime_seconds": uptime.total_seconds() if uptime else 0,
                "events_processed": self._stats["events_processed"],
                "person_events": self._stats["person_events"],
                "motion_events": self._stats["motion_events"],
                "files_cleaned": self._stats["files_cleaned"],
                "last_cleanup": self._stats["last_cleanup"],
                "storage": {
                    "free_gb": storage.free_gb,
                    "used_gb": storage.used_gb,
                    "total_gb": storage.total_gb,
                    "usage_percent": storage.usage_percent,
                },
                "security_system_status": self.security_system.status,
                "total_recordings": len(self.list_recordings()),
            }

        except Exception as e:
            self.logger.error(f"Error getting statistics: {e}")
            return {"error": str(e)}

    def get_timeline_plot(self, hours: int = 24) -> bytes:
        """Generate timeline plot for the specified hours."""
        return self.event_logger.create_timeline_plot(hours=hours)

    def get_analytics_summary(self, hours: int = 24) -> dict[str, Any]:
        """Get analytics summary for the specified hours."""
        return self.event_logger.get_summary_stats(hours=hours)

    def force_cleanup(self) -> int:
        """
        Force immediate cleanup of ALL event files.

        Returns:
            Number of files deleted
        """
        return self._cleanup_all_events()

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        self.stop()

    def __repr__(self) -> str:
        """String representation for debugging."""
        status = "running" if self.is_running else "stopped"
        return f"SecurityEventManager(status={status}, events={self._stats['events_processed']})"
