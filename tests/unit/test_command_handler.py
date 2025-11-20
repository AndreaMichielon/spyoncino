"""
Unit tests for DashboardCommandHandler module.

Tests the command processing logic for dashboard commands like snapshots,
timeline, and analytics.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spyoncino.core.bus import Subscription
from spyoncino.core.contracts import ControlCommand, Frame, ModuleConfig
from spyoncino.modules.dashboard.command_handler import DashboardCommandHandler


class DummyBus:
    """Mock event bus for testing."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []
        self.subscriptions: list[Subscription] = []
        self._handlers: dict[str, list[object]] = {}

    def subscribe(self, topic: str, handler: object) -> Subscription:
        sub = Subscription(topic=topic, handler=handler)  # type: ignore[arg-type]
        self.subscriptions.append(sub)
        if topic not in self._handlers:
            self._handlers[topic] = []
        self._handlers[topic].append(handler)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        self.subscriptions = [s for s in self.subscriptions if s is not subscription]
        # Remove handler from topic handlers
        topic = subscription.topic
        if topic in self._handlers:
            with contextlib.suppress(ValueError):
                self._handlers[topic].remove(subscription.handler)

    async def publish(self, topic: str, payload: object) -> None:
        self.published.append((topic, payload))
        # Call all handlers for this topic
        if topic in self._handlers:
            for handler in self._handlers[topic]:
                with contextlib.suppress(Exception):
                    await handler(topic, payload)


@pytest.mark.asyncio
async def test_snapshot_command_subscribes_to_frame_topic() -> None:
    """Test that snapshot command subscribes to the correct camera frame topic."""
    handler = DashboardCommandHandler()
    bus = DummyBus()
    handler.set_bus(bus)  # type: ignore[arg-type]

    await handler.configure(
        ModuleConfig(
            options={
                "command_topic": "dashboard.control.command",
                "snapshot_result_topic": "dashboard.snapshot.result",
            }
        )
    )
    await handler.start()

    cmd = ControlCommand(
        command="camera.snapshot",
        camera_id="default",
        arguments={"request_id": "snap-1"},
    )

    # Create a task that will publish a frame immediately
    async def publish_frame_immediately():
        await asyncio.sleep(0.01)  # Small delay to let subscription happen
        frame = Frame(
            camera_id="default",
            timestamp_utc=dt.datetime.now(tz=dt.UTC),
            image_bytes=b"fake-image-data",
            width=640,
            height=480,
        )
        await bus.publish("camera.default.frame", frame)

    # Start frame publisher and snapshot command concurrently
    await asyncio.gather(
        publish_frame_immediately(),
        handler._handle_snapshot_command(cmd),
    )

    # Should have published snapshot result
    snapshot_results = [(t, p) for t, p in bus.published if t == "dashboard.snapshot.result"]
    assert len(snapshot_results) == 1


@pytest.mark.asyncio
async def test_snapshot_command_publishes_result() -> None:
    """Test that snapshot command publishes result with image data."""
    handler = DashboardCommandHandler()
    bus = DummyBus()
    handler.set_bus(bus)  # type: ignore[arg-type]

    await handler.configure(
        ModuleConfig(
            options={
                "command_topic": "dashboard.control.command",
                "snapshot_result_topic": "dashboard.snapshot.result",
            }
        )
    )
    await handler.start()

    cmd = ControlCommand(
        command="camera.snapshot",
        camera_id="default",
        arguments={"request_id": "snap-1"},
    )

    # Create frame with proper datetime
    frame = Frame(
        camera_id="default",
        timestamp_utc=dt.datetime.now(tz=dt.UTC),
        image_bytes=b"fake-image-data",
        width=640,
        height=480,
    )

    # Publish frame and handle command concurrently
    async def publish_frame():
        await asyncio.sleep(0.01)
        await bus.publish("camera.default.frame", frame)

    await asyncio.gather(
        publish_frame(),
        handler._handle_snapshot_command(cmd),
    )

    # Should publish snapshot result as dict
    snapshot_results = [(t, p) for t, p in bus.published if t == "dashboard.snapshot.result"]
    assert len(snapshot_results) == 1
    topic, payload = snapshot_results[0]
    assert isinstance(payload, dict)
    assert payload["request_id"] == "snap-1"
    assert payload["camera_id"] == "default"
    assert payload["image_data"] == b"fake-image-data"


@pytest.mark.asyncio
async def test_timeline_command_generates_plot(tmp_path: Path) -> None:
    """Test that timeline command generates and publishes timeline plot."""
    handler = DashboardCommandHandler()
    bus = DummyBus()
    handler.set_bus(bus)  # type: ignore[arg-type]

    db_path = tmp_path / "events.db"
    db_path.touch()  # Create empty database

    await handler.configure(
        ModuleConfig(
            options={
                "command_topic": "dashboard.control.command",
                "timeline_result_topic": "dashboard.timeline.result",
                "events_db_path": str(db_path),
            }
        )
    )

    # Mock EventLogger
    with patch("spyoncino.modules.dashboard.command_handler.EventLogger") as mock_logger_class:
        mock_logger = MagicMock()
        mock_logger.create_timeline_plot.return_value = b"fake-plot-data"
        mock_logger_class.return_value = mock_logger

        await handler.start()

        cmd = ControlCommand(
            command="analytics.timeline",
            camera_id=None,
            arguments={"request_id": "timeline-1", "hours": 24},
        )

        await handler._handle_timeline_command(cmd)

    # Should publish timeline result as dict
    timeline_results = [(t, p) for t, p in bus.published if t == "dashboard.timeline.result"]
    assert len(timeline_results) == 1
    topic, payload = timeline_results[0]
    assert isinstance(payload, dict)
    assert payload["request_id"] == "timeline-1"
    assert payload["hours"] == 24
    assert payload["plot_data"] == b"fake-plot-data"


@pytest.mark.asyncio
async def test_analytics_command_generates_summary(tmp_path: Path) -> None:
    """Test that analytics command generates and publishes summary."""
    handler = DashboardCommandHandler()
    bus = DummyBus()
    handler.set_bus(bus)  # type: ignore[arg-type]

    db_path = tmp_path / "events.db"
    db_path.touch()

    await handler.configure(
        ModuleConfig(
            options={
                "command_topic": "dashboard.control.command",
                "analytics_result_topic": "dashboard.analytics.result",
                "events_db_path": str(db_path),
            }
        )
    )

    # Mock EventLogger
    with patch("spyoncino.modules.dashboard.command_handler.EventLogger") as mock_logger_class:
        mock_logger = MagicMock()
        mock_summary = {
            "total_events": 10,
            "motion_events": 5,
            "person_events": 5,
        }
        mock_logger.get_summary_stats.return_value = mock_summary
        mock_logger_class.return_value = mock_logger

        await handler.start()

        cmd = ControlCommand(
            command="analytics.summary",
            camera_id=None,
            arguments={"request_id": "analytics-1", "hours": 24},
        )

        await handler._handle_analytics_command(cmd)

    # Should publish analytics result as dict
    analytics_results = [(t, p) for t, p in bus.published if t == "dashboard.analytics.result"]
    assert len(analytics_results) == 1
    topic, payload = analytics_results[0]
    assert isinstance(payload, dict)
    assert payload["request_id"] == "analytics-1"
    assert payload["hours"] == 24
    assert payload["stats"] == mock_summary


@pytest.mark.asyncio
async def test_command_handler_ignores_non_control_commands() -> None:
    """Test that handler ignores payloads that aren't ControlCommand."""
    handler = DashboardCommandHandler()
    bus = DummyBus()
    handler.set_bus(bus)  # type: ignore[arg-type]

    await handler.configure(
        ModuleConfig(
            options={
                "command_topic": "dashboard.control.command",
            }
        )
    )
    await handler.start()

    # Send non-ControlCommand payload
    await handler._handle_command("dashboard.control.command", {"not": "a command"})

    # Should not publish anything
    assert len(bus.published) == 0


@pytest.mark.asyncio
async def test_command_handler_handles_unknown_commands() -> None:
    """Test that handler gracefully handles unknown commands."""
    handler = DashboardCommandHandler()
    bus = DummyBus()
    handler.set_bus(bus)  # type: ignore[arg-type]

    await handler.configure(
        ModuleConfig(
            options={
                "command_topic": "dashboard.control.command",
            }
        )
    )
    await handler.start()

    cmd = ControlCommand(
        command="unknown.command",
        camera_id=None,
        arguments={},
    )

    # Should not raise exception
    await handler._handle_command("dashboard.control.command", cmd)

    # Should not publish anything for unknown commands
    assert len(bus.published) == 0
