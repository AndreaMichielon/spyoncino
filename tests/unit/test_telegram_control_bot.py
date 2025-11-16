from __future__ import annotations

from types import SimpleNamespace

import pytest

from spyoncino.core.bus import Subscription
from spyoncino.core.contracts import ModuleConfig
from spyoncino.modules.dashboard.telegram_bot import TelegramControlBot


class DummyBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def subscribe(self, topic: str, handler: object) -> Subscription:
        return Subscription(topic=topic, handler=handler)  # type: ignore[arg-type]

    def unsubscribe(self, subscription: Subscription) -> None:  # pragma: no cover - no-op
        return None

    async def publish(self, topic: str, payload: object) -> None:
        self.published.append((topic, payload))


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_: object) -> None:
        self.replies.append(text)


class FakeChat:
    def __init__(self, *, chat_type: str = "private") -> None:
        self.id = 1
        self.type = chat_type
        self.messages: list[str] = []

    async def send_message(self, text: str, **_: object) -> None:
        self.messages.append(text)


class FakeUpdate:
    def __init__(self, *, user_id: int = 1, chat_type: str = "private") -> None:
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = FakeChat(chat_type=chat_type)
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, *, args: list[str] | None = None) -> None:
        self.args = args or []
        self.bot = None


@pytest.mark.asyncio
async def test_enable_command_publishes_control_message() -> None:
    bot = TelegramControlBot()
    bus = DummyBus()
    bot.set_bus(bus)  # type: ignore[arg-type]
    await bot.configure(
        ModuleConfig(
            options={
                "token": "123:ABC",
                "default_camera_id": "front",
                "command_topic": "dashboard.control.command",
                "user_whitelist": [7],
            }
        )
    )

    update = FakeUpdate(user_id=7)
    context = FakeContext(args=["garage"])

    await bot._cmd_enable(update, context)

    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == "dashboard.control.command"
    assert payload.command == "camera.state"
    assert payload.camera_id == "garage"
    assert payload.arguments["enabled"] is True
    assert any("Camera `garage` enabled." in msg for msg in update.message.replies)


@pytest.mark.asyncio
async def test_unauthorized_user_is_blocked_with_message() -> None:
    bot = TelegramControlBot()
    bus = DummyBus()
    bot.set_bus(bus)  # type: ignore[arg-type]
    await bot.configure(
        ModuleConfig(
            options={
                "token": "123:ABC",
                "default_camera_id": "front",
                "command_topic": "dashboard.control.command",
                "user_whitelist": [1],
                "silent_unauthorized": False,
            }
        )
    )

    update = FakeUpdate(user_id=99)
    context = FakeContext()
    await bot._cmd_status(update, context)

    assert bus.published == []
    assert update.message.replies[-1] == "You are not authorized to control this system."


@pytest.mark.asyncio
async def test_cleanup_command_publishes_storage_request() -> None:
    bot = TelegramControlBot()
    bus = DummyBus()
    bot.set_bus(bus)  # type: ignore[arg-type]
    await bot.configure(
        ModuleConfig(
            options={
                "token": "123:ABC",
                "default_camera_id": "front",
                "command_topic": "dashboard.control.command",
                "user_whitelist": [7],
            }
        )
    )

    update = FakeUpdate(user_id=7)
    context = FakeContext()

    await bot._cmd_cleanup(update, context)

    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == "dashboard.control.command"
    assert payload.command == "storage.cleanup"
    assert payload.camera_id is None


@pytest.mark.asyncio
async def test_start_monitor_command_supports_global_and_camera() -> None:
    bot = TelegramControlBot()
    bus = DummyBus()
    bot.set_bus(bus)  # type: ignore[arg-type]
    await bot.configure(
        ModuleConfig(
            options={
                "token": "123:ABC",
                "default_camera_id": "front",
                "command_topic": "dashboard.control.command",
                "user_whitelist": [7],
            }
        )
    )

    # Global (no camera arg, no default camera set in context)
    update_global = FakeUpdate(user_id=7)
    context_global = FakeContext(args=[])
    await bot._cmd_start_monitor(update_global, context_global)

    # Camera-specific
    update_cam = FakeUpdate(user_id=7)
    context_cam = FakeContext(args=["garage"])
    await bot._cmd_start_monitor(update_cam, context_cam)

    assert len(bus.published) == 2
    topic0, payload0 = bus.published[0]
    topic1, payload1 = bus.published[1]

    assert topic0 == "dashboard.control.command"
    assert payload0.command == "system.monitor.start"
    assert payload0.camera_id is None

    assert topic1 == "dashboard.control.command"
    assert payload1.command == "system.monitor.start"
    assert payload1.camera_id == "garage"


@pytest.mark.asyncio
async def test_recordings_command_attaches_request_id() -> None:
    bot = TelegramControlBot()
    bus = DummyBus()
    bot.set_bus(bus)  # type: ignore[arg-type]
    await bot.configure(
        ModuleConfig(
            options={
                "token": "123:ABC",
                "default_camera_id": "front",
                "command_topic": "dashboard.control.command",
                "user_whitelist": [7],
            }
        )
    )

    update = FakeUpdate(user_id=7)
    context = FakeContext(args=["front"])

    await bot._cmd_recordings(update, context)

    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == "dashboard.control.command"
    assert payload.command == "recordings.list"
    assert payload.camera_id == "front"
    # Request id must be present in arguments and tracked in bot state
    request_id = payload.arguments.get("request_id")
    assert isinstance(request_id, str) and request_id
    # Internal state should remember the request for later result handling
    assert request_id in bot._recordings_requests
