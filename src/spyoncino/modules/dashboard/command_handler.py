"""
Command handler module that processes dashboard control commands.

Handles commands like camera.snapshot, analytics.timeline, analytics.summary,
and system.monitor.start/stop by coordinating with other modules.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ...core.bus import Subscription
from ...core.contracts import BaseModule, ControlCommand, Frame, ModuleConfig
from ...legacy.analytics import EventLogger

logger = logging.getLogger(__name__)


class DashboardCommandHandler(BaseModule):
    """Handle dashboard control commands and publish results."""

    name = "modules.dashboard.command_handler"

    def __init__(self) -> None:
        super().__init__()
        self._command_topic = "dashboard.control.command"
        self._subscription: Subscription | None = None
        self._snapshot_result_topic = "dashboard.snapshot.result"
        self._timeline_result_topic = "dashboard.timeline.result"
        self._analytics_result_topic = "dashboard.analytics.result"
        # Default to same path as legacy analytics system
        self._events_db_path = Path("recordings") / "events.db"
        self._analytics_logger: EventLogger | None = None

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._command_topic = options.get("command_topic", self._command_topic)
        self._snapshot_result_topic = options.get(
            "snapshot_result_topic", self._snapshot_result_topic
        )
        self._timeline_result_topic = options.get(
            "timeline_result_topic", self._timeline_result_topic
        )
        self._analytics_result_topic = options.get(
            "analytics_result_topic", self._analytics_result_topic
        )
        db_path = options.get("events_db_path")
        if db_path:
            self._events_db_path = Path(db_path)
        else:
            # Try to infer from storage path if available
            storage_path = options.get("storage_path", "recordings")
            self._events_db_path = Path(storage_path) / "events.db"

    async def start(self) -> None:
        if self._subscription is not None:
            return
        self._subscription = self.bus.subscribe(self._command_topic, self._handle_command)

        # Initialize analytics logger - create database if it doesn't exist
        try:
            # Ensure parent directory exists
            self._events_db_path.parent.mkdir(parents=True, exist_ok=True)
            # Initialize EventLogger (it will create the database if needed)
            self._analytics_logger = EventLogger(db_path=str(self._events_db_path))
            logger.info("Analytics logger initialized with database at %s", self._events_db_path)
        except Exception as e:
            logger.warning("Failed to initialize analytics logger: %s", e)
            logger.warning(
                "Timeline and analytics commands will not work until database is available"
            )

        logger.info("DashboardCommandHandler listening for commands on %s", self._command_topic)

    async def stop(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription)
            self._subscription = None
        self._analytics_logger = None

    async def _handle_command(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, ControlCommand):
            logger.debug("Received non-ControlCommand payload on %s", topic)
            return

        command = payload.command
        logger.debug("Processing command: %s for camera %s", command, payload.camera_id)

        if command == "camera.snapshot":
            await self._handle_snapshot_command(payload)
        elif command == "analytics.timeline":
            await self._handle_timeline_command(payload)
        elif command == "analytics.summary":
            await self._handle_analytics_command(payload)
        elif command in ("system.monitor.start", "system.monitor.stop"):
            # These commands are handled by the orchestrator or other modules
            # We just acknowledge them here
            logger.debug("Received %s command for camera %s", command, payload.camera_id)
        elif command == "system.notification.test":
            # Test notification - could trigger a test event
            logger.info("Test notification command received")
        else:
            logger.debug("Unhandled command: %s", command)

    async def _handle_snapshot_command(self, cmd: ControlCommand) -> None:
        """Handle camera snapshot request by grabbing latest frame."""
        camera_id = cmd.camera_id
        if not camera_id:
            logger.warning("Snapshot command missing camera_id")
            return

        request_id = (cmd.arguments or {}).get("request_id")
        if not request_id:
            logger.warning("Snapshot command missing request_id")
            return

        logger.info(
            "Handling snapshot command for camera %s (request_id: %s)", camera_id, request_id
        )

        # Wait for a frame from the camera
        frame_topic = f"camera.{camera_id}.frame"
        frame_received = asyncio.Event()
        frame_data: Frame | None = None
        latest_frame: Frame | None = None

        async def frame_handler(topic: str, payload: Frame) -> None:
            if isinstance(payload, Frame) and payload.camera_id == camera_id:
                nonlocal frame_data, latest_frame
                # Always update latest_frame to get the most recent one
                latest_frame = payload
                # Set event on first frame received
                if frame_data is None:
                    frame_data = payload
                    frame_received.set()

        # Subscribe to camera frames temporarily
        frame_sub = self.bus.subscribe(frame_topic, frame_handler)
        try:
            # Wait for frame with timeout
            try:
                await asyncio.wait_for(frame_received.wait(), timeout=5.0)
                # Use latest frame if available (might be newer than first one)
                if latest_frame is not None:
                    frame_data = latest_frame
            except TimeoutError:
                logger.warning(
                    "Timeout waiting for frame from camera %s (topic: %s)", camera_id, frame_topic
                )
                # Try to publish error result
                error_result = {
                    "request_id": request_id,
                    "camera_id": camera_id,
                    "error": "Timeout waiting for camera frame",
                }
                await self.bus.publish(self._snapshot_result_topic, error_result)
                return

            if frame_data is None:
                logger.warning("No frame data received for camera %s", camera_id)
                error_result = {
                    "request_id": request_id,
                    "camera_id": camera_id,
                    "error": "No frame data available",
                }
                await self.bus.publish(self._snapshot_result_topic, error_result)
                return

            # Read frame data
            image_bytes = None
            content_type = frame_data.content_type or "image/jpeg"

            if frame_data.image_bytes:
                image_bytes = frame_data.image_bytes
            elif frame_data.data_ref:
                path = Path(frame_data.data_ref)
                if path.exists():
                    image_bytes = path.read_bytes()
                    # Infer content type from extension if not set
                    if not frame_data.content_type:
                        ext = path.suffix.lower()
                        if ext in (".jpg", ".jpeg"):
                            content_type = "image/jpeg"
                        elif ext == ".png":
                            content_type = "image/png"

            if image_bytes:
                # Publish snapshot result as a dict (telegram bot will handle it)
                result = {
                    "request_id": request_id,
                    "camera_id": camera_id,
                    "image_data": image_bytes,
                    "content_type": content_type,
                }
                await self.bus.publish(self._snapshot_result_topic, result)
                logger.info("Published snapshot result for camera %s", camera_id)
            else:
                logger.warning("No image data available for snapshot from camera %s", camera_id)
        finally:
            self.bus.unsubscribe(frame_sub)

    async def _handle_timeline_command(self, cmd: ControlCommand) -> None:
        """Handle analytics timeline request."""
        if not self._analytics_logger:
            logger.warning("Analytics logger not available for timeline generation")
            return

        args = cmd.arguments or {}
        hours = int(args.get("hours", 24))
        request_id = args.get("request_id")
        if not request_id:
            logger.warning("Timeline command missing request_id")
            return

        try:
            # Generate timeline plot in thread
            plot_data = await asyncio.to_thread(
                self._analytics_logger.create_timeline_plot, hours=hours
            )

            if plot_data:
                result = {
                    "request_id": request_id,
                    "plot_data": plot_data,
                    "hours": hours,
                }
                await self.bus.publish(self._timeline_result_topic, result)
                logger.info("Published timeline result for %d hours", hours)
            else:
                logger.warning("Timeline plot generation returned no data")
        except Exception as e:
            logger.error("Failed to generate timeline plot: %s", e, exc_info=True)

    async def _handle_analytics_command(self, cmd: ControlCommand) -> None:
        """Handle analytics summary request."""
        if not self._analytics_logger:
            logger.warning("Analytics logger not available for analytics summary")
            return

        args = cmd.arguments or {}
        hours = int(args.get("hours", 24))
        request_id = args.get("request_id")
        if not request_id:
            logger.warning("Analytics command missing request_id")
            return

        try:
            # Get analytics summary in thread
            stats = await asyncio.to_thread(self._analytics_logger.get_summary_stats, hours=hours)

            if stats:
                result = {
                    "request_id": request_id,
                    "stats": stats,
                    "hours": hours,
                }
                await self.bus.publish(self._analytics_result_topic, result)
                logger.info("Published analytics result for %d hours", hours)
            else:
                logger.warning("Analytics summary generation returned no data")
        except Exception as e:
            logger.error("Failed to generate analytics summary: %s", e, exc_info=True)


__all__ = ["DashboardCommandHandler"]
