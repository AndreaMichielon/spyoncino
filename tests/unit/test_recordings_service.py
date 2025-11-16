from __future__ import annotations

from pathlib import Path

import pytest

from spyoncino.core.bus import Subscription
from spyoncino.core.contracts import ControlCommand, ModuleConfig
from spyoncino.modules.dashboard.recordings_service import RecordingsService


class DummyBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []
        self.subscriptions: list[Subscription] = []

    def subscribe(self, topic: str, handler: object) -> Subscription:
        sub = Subscription(topic=topic, handler=handler)  # type: ignore[arg-type]
        self.subscriptions.append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:  # pragma: no cover - no-op
        self.subscriptions = [s for s in self.subscriptions if s is not subscription]

    async def publish(self, topic: str, payload: object) -> None:
        self.published.append((topic, payload))


@pytest.mark.asyncio
async def test_recordings_list_builds_items_from_files(tmp_path: Path) -> None:
    # Create a few fake GIF recordings
    paths = [
        tmp_path / "motion_20250101_120000.gif",
        tmp_path / "person_20250101_121500.gif",
    ]
    for p in paths:
        p.write_bytes(b"gif-bytes")

    svc = RecordingsService()
    bus = DummyBus()
    svc.set_bus(bus)  # type: ignore[arg-type]

    await svc.configure(
        ModuleConfig(
            options={
                "events_root": str(tmp_path),
                "command_topic": "dashboard.control.command",
                "list_result_topic": "dashboard.recordings.list.result",
                "get_result_topic": "dashboard.recordings.get.result",
            }
        )
    )

    # Call handler directly with a ControlCommand
    cmd = ControlCommand(
        command="recordings.list",
        camera_id=None,
        arguments={"request_id": "req-1", "limit": 10},
    )
    await svc._handle_list_command(cmd)

    # Expect one list result published
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == "dashboard.recordings.list.result"
    assert payload.request_id == "req-1"
    assert len(payload.items) == 2
    ids = {item.id for item in payload.items}
    assert "motion_20250101_120000" in ids
    assert "person_20250101_121500" in ids


@pytest.mark.asyncio
async def test_recordings_get_resolves_stem_to_path(tmp_path: Path) -> None:
    gif_path = tmp_path / "person_20250101_121500.gif"
    gif_path.write_bytes(b"gif-bytes")

    svc = RecordingsService()
    bus = DummyBus()
    svc.set_bus(bus)  # type: ignore[arg-type]

    await svc.configure(
        ModuleConfig(
            options={
                "events_root": str(tmp_path),
                "command_topic": "dashboard.control.command",
                "list_result_topic": "dashboard.recordings.list.result",
                "get_result_topic": "dashboard.recordings.get.result",
            }
        )
    )

    cmd = ControlCommand(
        command="recordings.get",
        camera_id=None,
        arguments={"request_id": "req-2", "item_id": "person_20250101_121500"},
    )
    await svc._handle_get_command(cmd)

    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == "dashboard.recordings.get.result"
    assert payload.request_id == "req-2"
    assert payload.item_id == "person_20250101_121500"
    assert Path(payload.path) == gif_path
    assert payload.content_type == "image/gif"


@pytest.mark.asyncio
async def test_recordings_get_latest_for_camera(tmp_path: Path) -> None:
    # Two recordings for same camera at different times
    older = tmp_path / "front_motion_20250101_120000.gif"
    newer = tmp_path / "front_motion_20250101_121500.gif"
    older.write_bytes(b"gif-bytes-1")
    newer.write_bytes(b"gif-bytes-2")

    svc = RecordingsService()
    bus = DummyBus()
    svc.set_bus(bus)  # type: ignore[arg-type]

    await svc.configure(
        ModuleConfig(
            options={
                "events_root": str(tmp_path),
                "command_topic": "dashboard.control.command",
                "list_result_topic": "dashboard.recordings.list.result",
                "get_result_topic": "dashboard.recordings.get.result",
            }
        )
    )

    cmd = ControlCommand(
        command="recordings.get",
        camera_id="front",
        arguments={"request_id": "req-3", "mode": "latest_for_camera"},
    )
    await svc._handle_get_command(cmd)

    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == "dashboard.recordings.get.result"
    assert payload.request_id == "req-3"
    # Should pick the newer file for this camera
    assert Path(payload.path) == newer
