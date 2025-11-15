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
