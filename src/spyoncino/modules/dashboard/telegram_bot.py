"""
Telegram-based control surface that mirrors the legacy bot capabilities.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from ...core.bus import Subscription
from ...core.contracts import BaseModule, ControlCommand, HealthStatus, HealthSummary, ModuleConfig

logger = logging.getLogger(__name__)

try:  # pragma: no cover - optional dependency guard
    from telegram.ext import Application, CommandHandler
except ModuleNotFoundError:  # pragma: no cover - handled at runtime
    Application = None
    CommandHandler = None


class TelegramUpdaterProto(Protocol):
    async def start_polling(self, *args: Any, **kwargs: Any) -> None: ...

    async def stop(self) -> None: ...


class TelegramApplicationProto(Protocol):
    updater: TelegramUpdaterProto | None

    def add_handler(self, handler: Any) -> None: ...

    async def initialize(self) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def shutdown(self) -> None: ...


AppFactory = Callable[[str], TelegramApplicationProto]


class TelegramControlBot(BaseModule):
    """Expose Telegram commands that publish control messages to the bus."""

    name = "modules.dashboard.telegram_bot"

    def __init__(
        self,
        *,
        app_factory: AppFactory | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self._app_factory = app_factory
        self._clock = clock or time.monotonic
        self._application: TelegramApplicationProto | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._health_subscription: Subscription | None = None
        self._health_topic = "status.health.summary"
        self._command_topic = "dashboard.control.command"
        self._last_health: HealthSummary | None = None
        self._default_camera_id: str | None = None
        self._token: str | None = None
        self._user_whitelist: set[int] = set()
        self._superuser_id: int | None = None
        self._setup_password: str | None = None
        self._allow_group_commands = True
        self._silent_unauthorized = True
        self._send_typing_action = True
        self._command_rate_limit = 10
        self._command_window_seconds = 60.0
        self._command_usage: dict[int, deque[float]] = {}
        self._bot_running = False

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._token = options.get("token") or self._token
        self._default_camera_id = options.get("default_camera_id", self._default_camera_id)
        self._command_topic = options.get("command_topic", self._command_topic)
        self._health_topic = options.get("health_topic", self._health_topic)
        self._user_whitelist = {int(x) for x in options.get("user_whitelist", []) if x is not None}
        if options.get("superuser_id") is not None:
            self._superuser_id = int(options["superuser_id"])
        self._setup_password = options.get("setup_password", self._setup_password)
        self._allow_group_commands = bool(
            options.get("allow_group_commands", self._allow_group_commands)
        )
        self._silent_unauthorized = bool(
            options.get("silent_unauthorized", self._silent_unauthorized)
        )
        self._command_rate_limit = int(options.get("command_rate_limit", self._command_rate_limit))
        self._send_typing_action = bool(options.get("send_typing_action", self._send_typing_action))

    async def start(self) -> None:
        if not self._token:
            logger.warning("TelegramControlBot cannot start without a bot token.")
            return
        if self._application is not None:
            logger.debug("TelegramControlBot already running.")
            return
        if self._app_factory:
            self._application = self._app_factory(self._token)
        else:
            if Application is None or CommandHandler is None:
                logger.error(
                    "python-telegram-bot is not installed; TelegramControlBot will stay disabled."
                )
                return
            self._application = Application.builder().token(self._token).build()
        self._register_handlers()
        self._shutdown_event = asyncio.Event()
        self._runner_task = asyncio.create_task(
            self._run_application(), name="telegram-control-bot"
        )
        self._health_subscription = self.bus.subscribe(
            self._health_topic, self._handle_health_event
        )
        logger.info("TelegramControlBot started.")

    async def stop(self) -> None:
        if self._health_subscription:
            self.bus.unsubscribe(self._health_subscription)
            self._health_subscription = None
        if self._shutdown_event:
            self._shutdown_event.set()
        if self._runner_task:
            await self._runner_task
            self._runner_task = None
        self._application = None
        self._bot_running = False
        logger.info("TelegramControlBot stopped.")

    async def health(self) -> HealthStatus:
        status = "healthy" if self._bot_running else "degraded"
        return HealthStatus(
            status=status,
            details={
                "bot_running": self._bot_running,
                "seen_health_events": self._last_health is not None,
                "authorized_users": len(self._user_whitelist),
            },
        )

    def _register_handlers(self) -> None:
        if self._application is None or CommandHandler is None:
            return
        handlers = [
            ("start", self._cmd_start),
            ("help", self._cmd_help),
            ("status", self._cmd_status),
            ("enable", self._cmd_enable),
            ("disable", self._cmd_disable),
            ("snapshot", self._cmd_snapshot),
            ("setup", self._cmd_setup),
            ("whoami", self._cmd_whoami),
        ]
        for name, handler in handlers:
            self._application.add_handler(CommandHandler(name, handler))

    async def _run_application(self) -> None:
        assert self._application is not None
        updater = getattr(self._application, "updater", None)
        try:
            await self._application.initialize()
            await self._application.start()
            if updater:
                await updater.start_polling(drop_pending_updates=True)
            self._bot_running = True
            if self._shutdown_event:
                await self._shutdown_event.wait()
        finally:
            if updater:
                with contextlib.suppress(Exception):
                    await updater.stop()
            with contextlib.suppress(Exception):
                await self._application.stop()
            with contextlib.suppress(Exception):
                await self._application.shutdown()
            self._bot_running = False

    async def _handle_health_event(self, topic: str, payload: HealthSummary) -> None:
        if isinstance(payload, HealthSummary):
            self._last_health = payload

    async def _cmd_start(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        await self._respond(
            update,
            "👋 Hi! I'm your Spyoncino control bot.\n" "Use /help to see the available commands.",
        )

    async def _cmd_help(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        await self._respond(
            update,
            (
                "📋 Available commands:\n"
                "/status - Show system health summary\n"
                "/enable [camera_id] - Enable a camera\n"
                "/disable [camera_id] - Disable a camera\n"
                "/snapshot [camera_id] - Request a snapshot\n"
                "/whoami - Display your Telegram user id\n"
                "/setup <password> - Claim superuser access (first run)"
            ),
        )

    async def _cmd_status(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        if not self._last_health:
            await self._respond(update, "No health reports yet. Please try again shortly.")
            return
        lines = [f"System status: {self._last_health.status.upper()}"]
        for module, report in sorted(self._last_health.modules.items()):
            lines.append(f"• {module}: {report.status}")
        await self._respond(update, "\n".join(lines))

    async def _cmd_enable(self, update: Any, context: Any) -> None:
        await self._handle_camera_state(update, context, enabled=True)

    async def _cmd_disable(self, update: Any, context: Any) -> None:
        await self._handle_camera_state(update, context, enabled=False)

    async def _cmd_snapshot(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        camera_id = self._camera_from_context(context)
        if not camera_id:
            await self._respond(update, "No camera id provided and no default camera configured.")
            return
        await self._publish_command(
            ControlCommand(command="camera.snapshot", camera_id=camera_id, arguments={})
        )
        await self._respond(update, f"Snapshot requested for camera `{camera_id}`.")

    async def _cmd_setup(self, update: Any, context: Any) -> None:
        if self._user_whitelist and not await self._ensure_command_allowed(update, context):
            return
        if not self._setup_password:
            await self._respond(update, "Setup password is not configured.")
            return
        if self._user_whitelist:
            await self._respond(update, "Setup already completed.")
            return
        args = self._context_args(context)
        if not args:
            await self._respond(update, "Usage: /setup <password>")
            return
        if args[0] != self._setup_password:
            await self._respond(update, "Invalid setup password.")
            return
        user_id = self._user_id(update)
        if user_id is None:
            await self._respond(update, "Unable to determine your Telegram user id.")
            return
        self._user_whitelist.add(user_id)
        self._superuser_id = user_id
        await self._respond(update, "Setup complete. You now have administrative access.")

    async def _cmd_whoami(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        user_id = self._user_id(update)
        await self._respond(update, f"Your Telegram user id is `{user_id}`.")

    async def _handle_camera_state(
        self,
        update: Any,
        context: Any,
        *,
        enabled: bool,
    ) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        camera_id = self._camera_from_context(context)
        if not camera_id:
            await self._respond(update, "No camera id provided and no default camera configured.")
            return
        await self._publish_command(
            ControlCommand(
                command="camera.state",
                camera_id=camera_id,
                arguments={"enabled": enabled},
            )
        )
        verb = "enabled" if enabled else "disabled"
        await self._respond(update, f"Camera `{camera_id}` {verb}.")

    async def _ensure_command_allowed(self, update: Any, context: Any) -> bool:
        user_id = self._user_id(update)
        if user_id is None:
            if not self._silent_unauthorized:
                await self._respond(update, "Cannot determine user identity.")
            return False
        if self._is_group_context(update) and not self._allow_group_commands:
            if not self._silent_unauthorized:
                await self._respond(update, "Commands are disabled in group chats.")
            return False
        if not self._is_authorized(user_id):
            if not self._silent_unauthorized:
                await self._respond(update, "You are not authorized to control this system.")
            return False
        if not self._check_rate_limit(user_id):
            if not self._silent_unauthorized:
                await self._respond(update, "Rate limit exceeded. Please slow down.")
            return False
        await self._send_typing_indicator(update, context)
        return True

    def _is_authorized(self, user_id: int) -> bool:
        if user_id == self._superuser_id:
            return True
        if not self._user_whitelist:
            return True
        return user_id in self._user_whitelist

    def _check_rate_limit(self, user_id: int) -> bool:
        if self._command_rate_limit <= 0:
            return True
        bucket = self._command_usage.setdefault(user_id, deque())
        now = self._clock()
        while bucket and now - bucket[0] > self._command_window_seconds:
            bucket.popleft()
        if len(bucket) >= self._command_rate_limit:
            return False
        bucket.append(now)
        return True

    async def _publish_command(self, command: ControlCommand) -> None:
        await self.bus.publish(self._command_topic, command)
        logger.info(
            "TelegramControlBot published command %s for camera %s",
            command.command,
            command.camera_id,
        )

    async def _respond(self, update: Any, message: str) -> None:
        target = getattr(update, "message", None)
        if target and hasattr(target, "reply_text"):
            await target.reply_text(message, parse_mode="Markdown")
            return
        chat = getattr(update, "effective_chat", None)
        if chat and hasattr(chat, "send_message"):
            await chat.send_message(message, parse_mode="Markdown")

    async def _send_typing_indicator(self, update: Any, context: Any) -> None:
        if not self._send_typing_action:
            return
        bot = getattr(context, "bot", None)
        chat = getattr(update, "effective_chat", None)
        if not bot or not chat or not hasattr(bot, "send_chat_action"):
            return
        try:
            await bot.send_chat_action(chat.id, action="typing")
        except Exception:  # pragma: no cover - network errors are non-deterministic
            logger.debug("Failed to send typing indicator", exc_info=True)

    def _camera_from_context(self, context: Any) -> str | None:
        args = self._context_args(context)
        if args:
            return args[0]
        return self._default_camera_id

    def _context_args(self, context: Any) -> list[str]:
        args = getattr(context, "args", None)
        if not args:
            return []
        return list(args)

    def _user_id(self, update: Any) -> int | None:
        user = getattr(update, "effective_user", None)
        if not user:
            return None
        return getattr(user, "id", None)

    def _is_group_context(self, update: Any) -> bool:
        chat = getattr(update, "effective_chat", None)
        chat_type = getattr(chat, "type", "")
        return str(chat_type).lower() in {"group", "supergroup"}


__all__ = ["TelegramControlBot"]
