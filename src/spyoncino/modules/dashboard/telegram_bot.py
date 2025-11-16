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
from ...core.contracts import (
    BaseModule,
    ControlCommand,
    HealthStatus,
    HealthSummary,
    ModuleConfig,
    RecordingGetResult,
    RecordingListItem,
    RecordingsListResult,
    StorageStats,
)

logger = logging.getLogger(__name__)

try:  # pragma: no cover - optional dependency guard
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler
except ModuleNotFoundError:  # pragma: no cover - handled at runtime
    Application = None
    CommandHandler = None
    CallbackQueryHandler = None
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None


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
        self._storage_subscription: Subscription | None = None
        self._recordings_list_subscription: Subscription | None = None
        self._recording_get_subscription: Subscription | None = None
        self._health_topic = "status.health.summary"
        self._storage_topic = "storage.stats"
        self._command_topic = "dashboard.control.command"
        self._recordings_list_topic = "dashboard.recordings.list.result"
        self._recording_get_topic = "dashboard.recordings.get.result"
        self._last_health: HealthSummary | None = None
        self._last_storage: StorageStats | None = None
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
        # Correlate recording list/get flows
        self._recordings_requests: dict[str, dict[str, Any]] = {}

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._token = options.get("token") or self._token
        self._default_camera_id = options.get("default_camera_id", self._default_camera_id)
        self._command_topic = options.get("command_topic", self._command_topic)
        self._health_topic = options.get("health_topic", self._health_topic)
        self._storage_topic = options.get("storage_topic", self._storage_topic)
        self._recordings_list_topic = options.get(
            "recordings_list_topic", self._recordings_list_topic
        )
        self._recording_get_topic = options.get("recording_get_topic", self._recording_get_topic)
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
        self._storage_subscription = self.bus.subscribe(
            self._storage_topic, self._handle_storage_event
        )
        self._recordings_list_subscription = self.bus.subscribe(
            self._recordings_list_topic, self._handle_recordings_list_result
        )
        self._recording_get_subscription = self.bus.subscribe(
            self._recording_get_topic, self._handle_recording_get_result
        )
        logger.info("TelegramControlBot started.")

    async def stop(self) -> None:
        if self._health_subscription:
            self.bus.unsubscribe(self._health_subscription)
            self._health_subscription = None
        if self._storage_subscription:
            self.bus.unsubscribe(self._storage_subscription)
            self._storage_subscription = None
        if self._recordings_list_subscription:
            self.bus.unsubscribe(self._recordings_list_subscription)
            self._recordings_list_subscription = None
        if self._recording_get_subscription:
            self.bus.unsubscribe(self._recording_get_subscription)
            self._recording_get_subscription = None
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
            ("stats", self._cmd_stats),
            ("enable", self._cmd_enable),
            ("disable", self._cmd_disable),
            ("snapshot", self._cmd_snapshot),
            ("snap", self._cmd_snapshot),
            ("cleanup", self._cmd_cleanup),
            ("setup", self._cmd_setup),
            ("whoami", self._cmd_whoami),
            ("start_monitor", self._cmd_start_monitor),
            ("stop_monitor", self._cmd_stop_monitor),
            ("recordings", self._cmd_recordings),
            ("get", self._cmd_get_recording),
            ("config", self._cmd_config),
            ("show_config", self._cmd_show_config),
            ("test", self._cmd_test_notification),
            ("timeline", self._cmd_timeline),
            ("analytics", self._cmd_analytics),
            ("whitelist_add", self._cmd_whitelist_add),
            ("whitelist_remove", self._cmd_whitelist_remove),
            ("whitelist_list", self._cmd_whitelist_list),
        ]
        for name, handler in handlers:
            self._application.add_handler(CommandHandler(name, handler))
        # Callback handler for interactive recordings keyboard
        if CallbackQueryHandler is not None:
            self._application.add_handler(
                CallbackQueryHandler(self._handle_callback_query, pattern=r"^rec:")
            )

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

    async def _handle_storage_event(self, topic: str, payload: StorageStats) -> None:
        if isinstance(payload, StorageStats):
            self._last_storage = payload

    async def _handle_recordings_list_result(self, topic: str, payload: Any) -> None:
        """Render an inline keyboard for a recordings list result."""
        if not isinstance(payload, RecordingsListResult):
            return
        request_id = payload.request_id
        context = self._recordings_requests.get(request_id)
        if not context:
            return
        chat_id = context.get("chat_id")
        if chat_id is None or self._application is None:
            return
        bot = getattr(self._application, "bot", None)
        if bot is None or InlineKeyboardButton is None or InlineKeyboardMarkup is None:
            return

        # Group items by date into Today / Yesterday / Older buckets
        import datetime as dt

        today = dt.datetime.now(tz=dt.UTC).date()
        yesterday = today - dt.timedelta(days=1)

        today_items: list[RecordingListItem] = []
        yesterday_items: list[RecordingListItem] = []
        older_items: list[RecordingListItem] = []

        for item in payload.items:
            ts = item.timestamp_utc
            if ts is None:
                older_items.append(item)
                continue
            local_date = ts.astimezone(dt.UTC).date()
            if local_date == today:
                today_items.append(item)
            elif local_date == yesterday:
                yesterday_items.append(item)
            else:
                older_items.append(item)

        async def send_group(title: str, items: list[RecordingListItem]) -> None:
            if not items:
                return
            buttons: list[Any] = []
            for item in items:
                callback_data = f"rec:{request_id}:{item.id}"
                buttons.append(InlineKeyboardButton(item.label, callback_data=callback_data))
            rows: list[list[Any]] = []
            for i in range(0, len(buttons), 4):
                rows.append(buttons[i : i + 4])
            if not rows:
                return
            markup = InlineKeyboardMarkup(rows)
            await bot.send_message(chat_id=chat_id, text=title, reply_markup=markup)

        # Send grouped keyboards, most relevant (Today) first
        if not (today_items or yesterday_items or older_items):
            await bot.send_message(chat_id=chat_id, text="No recordings available.")
            return

        await send_group("📅 Today", today_items)
        await send_group("📅 Yesterday", yesterday_items)

        if older_items:
            # Compute rough date range for older items
            dates = [
                i.timestamp_utc.astimezone(dt.UTC).date()
                for i in older_items
                if i.timestamp_utc is not None
            ]
            if dates:
                start = min(dates).strftime("%m/%d")
                end = max(dates).strftime("%m/%d")
                subtitle = f" ({start})" if start == end else f" ({start}-{end})"
            else:
                subtitle = ""
            await send_group(f"📅 Older{subtitle}", older_items)

    async def _handle_recording_get_result(self, topic: str, payload: Any) -> None:
        """Send the requested recording back to the originating chat."""
        if not isinstance(payload, RecordingGetResult):
            return
        request_id = payload.request_id
        context = self._recordings_requests.get(request_id)
        if not context or self._application is None:
            return
        chat_id = context.get("chat_id")
        if chat_id is None:
            return
        bot = getattr(self._application, "bot", None)
        if bot is None:
            return

        from pathlib import Path

        path = Path(payload.path)
        if not path.exists():
            logger.warning("RecordingGetResult path %s does not exist", path)
            return

        # Decide how to send based on MIME type
        content_type = (payload.content_type or "").lower()
        method_name = "send_animation"
        if content_type.startswith("video/"):
            method_name = "send_video"
        sender = getattr(bot, method_name, None)
        if sender is None:
            return

        try:
            with path.open("rb") as f:
                await sender(
                    chat_id=chat_id, animation=f
                ) if method_name == "send_animation" else await sender(  # type: ignore[arg-type]
                    chat_id=chat_id, video=f
                )
        except Exception:  # pragma: no cover - network and IO errors are non-deterministic
            logger.exception("Failed to send recording %s to chat %s", path, chat_id)

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

    async def _cmd_stats(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        lines = []
        if self._last_health:
            lines.append(f"System status: {self._last_health.status.upper()}")
        if self._last_storage:
            storage = self._last_storage
            lines.append(
                f"Storage: {storage.used_gb:.2f}GB / {storage.total_gb:.2f}GB "
                f"({storage.usage_percent:.1f}% used)"
            )
        if not lines:
            lines.append("No telemetry received yet. Please try again soon.")
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

    async def _cmd_cleanup(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        await self._publish_command(
            ControlCommand(
                command="storage.cleanup",
                camera_id=None,
                arguments={"mode": "aggressive"},
            )
        )
        await self._respond(update, "Storage cleanup requested.")

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

    async def _cmd_start_monitor(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        # start_monitor without args applies globally; with a camera id it is scoped.
        args = self._context_args(context)
        camera_id = args[0] if args else None
        await self._publish_command(
            ControlCommand(
                command="system.monitor.start",
                camera_id=camera_id,
                arguments={},
            )
        )
        if camera_id:
            await self._respond(update, f"Monitoring started for camera `{camera_id}`.")
        else:
            await self._respond(update, "Monitoring started for all cameras.")

    async def _cmd_stop_monitor(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        args = self._context_args(context)
        camera_id = args[0] if args else None
        await self._publish_command(
            ControlCommand(
                command="system.monitor.stop",
                camera_id=camera_id,
                arguments={},
            )
        )
        if camera_id:
            await self._respond(update, f"Monitoring stopped for camera `{camera_id}`.")
        else:
            await self._respond(update, "Monitoring stopped for all cameras.")

    async def _cmd_recordings(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        args = self._context_args(context)
        camera_id = args[0] if args else None
        # Generate a simple request id for correlating with results
        request_id = f"rec-{int(self._clock() * 1000)}-{len(self._recordings_requests) + 1}"
        # Store context needed to render the inline keyboard when results arrive
        chat = getattr(update, "effective_chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id is not None:
            self._recordings_requests[request_id] = {"chat_id": chat_id}
        await self._publish_command(
            ControlCommand(
                command="recordings.list",
                camera_id=camera_id or None,
                arguments={"request_id": request_id},
            )
        )
        if camera_id:
            await self._respond(
                update, f"Requested recordings list for camera `{camera_id}` from dashboard."
            )
        else:
            await self._respond(
                update,
                "Requested global recordings list from dashboard. " "Results will appear shortly.",
            )

    async def _cmd_get_recording(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        args = self._context_args(context)
        if not args:
            await self._respond(update, "Usage: /get <index|event_name>")
            return
        key = args[0]

        # Heuristic: if the argument looks like a bare camera identifier (no dots/underscores),
        # treat it as "latest recording for this camera" instead of a raw event name.
        if all(ch.isalnum() or ch in "-@" for ch in key) and "_" not in key and "." not in key:
            camera_id = key
            await self._publish_command(
                ControlCommand(
                    command="recordings.get",
                    camera_id=camera_id,
                    arguments={"mode": "latest_for_camera"},
                )
            )
            await self._respond(
                update, f"Requested latest recording for camera `{camera_id}` from dashboard."
            )
            return

        # Fallback: treat key as event/recording name (filename stem)
        await self._publish_command(
            ControlCommand(
                command="recordings.get",
                camera_id=None,
                arguments={"key": key},
            )
        )
        await self._respond(update, f"Requested recording `{key}` from dashboard.")

    async def _cmd_show_config(self, update: Any, context: Any) -> None:
        # Show-config is a superuser-only, bot-local command for now.
        if not await self._ensure_superuser(update, context):
            return
        enabled_users = ", ".join(str(uid) for uid in sorted(self._user_whitelist)) or "none"
        await self._respond(
            update,
            (
                "Current Telegram control bot configuration:\n"
                f"- default_camera_id: `{self._default_camera_id}`\n"
                f"- command_topic: `{self._command_topic}`\n"
                f"- allow_group_commands: `{self._allow_group_commands}`\n"
                f"- silent_unauthorized: `{self._silent_unauthorized}`\n"
                f"- command_rate_limit: `{self._command_rate_limit}` per {int(self._command_window_seconds)}s\n"
                f"- superuser_id: `{self._superuser_id}`\n"
                f"- whitelisted_users: {enabled_users}"
            ),
        )

    async def _cmd_config(self, update: Any, context: Any) -> None:
        # Configuration changes are treated as admin-only, forwarded as control commands.
        if not await self._ensure_superuser(update, context):
            return
        args = self._context_args(context)
        if len(args) < 2:
            await self._respond(
                update,
                "Usage: /config <key> <value>\n"
                "This forwards a generic configuration request to the dashboard backend.",
            )
            return
        key, value = args[0], " ".join(args[1:])
        await self._publish_command(
            ControlCommand(
                command="config.update",
                camera_id=None,
                arguments={"key": key, "value": value},
            )
        )
        await self._respond(
            update, f"Forwarded configuration update request `{key}={value}` to dashboard."
        )

    async def _cmd_test_notification(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        await self._publish_command(
            ControlCommand(
                command="system.notification.test",
                camera_id=None,
                arguments={},
            )
        )
        await self._respond(update, "Test notification command sent to dashboard.")

    async def _cmd_timeline(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        args = self._context_args(context)
        hours = 24
        if args:
            try:
                hours = max(1, min(168, int(args[0])))
            except ValueError:
                await self._respond(update, "Invalid hours value. Using default (24h).")
                hours = 24
        await self._publish_command(
            ControlCommand(
                command="analytics.timeline",
                camera_id=None,
                arguments={"hours": hours},
            )
        )
        await self._respond(update, f"Requested analytics timeline for the last {hours} hours.")

    async def _cmd_analytics(self, update: Any, context: Any) -> None:
        if not await self._ensure_command_allowed(update, context):
            return
        args = self._context_args(context)
        hours = 24
        if args:
            try:
                hours = max(1, min(168, int(args[0])))
            except ValueError:
                await self._respond(update, "Invalid hours value. Using default (24h).")
                hours = 24
        await self._publish_command(
            ControlCommand(
                command="analytics.summary",
                camera_id=None,
                arguments={"hours": hours},
            )
        )
        await self._respond(update, f"Requested analytics summary for the last {hours} hours.")

    async def _cmd_whitelist_add(self, update: Any, context: Any) -> None:
        if not await self._ensure_superuser(update, context):
            return
        args = self._context_args(context)
        if not args:
            await self._respond(update, "Usage: /whitelist_add <user_id>")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await self._respond(update, "Invalid user id. It must be a number.")
            return
        if user_id <= 0:
            await self._respond(update, "User id must be positive.")
            return
        if user_id in self._user_whitelist:
            await self._respond(update, f"User `{user_id}` is already whitelisted.")
            return
        self._user_whitelist.add(user_id)
        await self._respond(update, f"User `{user_id}` added to whitelist.")

    async def _cmd_whitelist_remove(self, update: Any, context: Any) -> None:
        if not await self._ensure_superuser(update, context):
            return
        args = self._context_args(context)
        if not args:
            await self._respond(update, "Usage: /whitelist_remove <user_id>")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await self._respond(update, "Invalid user id. It must be a number.")
            return
        if user_id not in self._user_whitelist:
            await self._respond(update, f"User `{user_id}` is not in the whitelist.")
            return
        self._user_whitelist.remove(user_id)
        await self._respond(update, f"User `{user_id}` removed from whitelist.")

    async def _cmd_whitelist_list(self, update: Any, context: Any) -> None:
        if not await self._ensure_superuser(update, context):
            return
        if not self._user_whitelist:
            await self._respond(update, "Whitelist is currently empty (all users allowed).")
            return
        users = ", ".join(f"`{uid}`" for uid in sorted(self._user_whitelist))
        await self._respond(update, f"Whitelisted users: {users}")

    async def _handle_callback_query(self, update: Any, context: Any) -> None:
        """Handle inline button presses for recordings selection."""
        query = getattr(update, "callback_query", None)
        if not query or not hasattr(query, "data"):
            return
        data = str(getattr(query, "data", ""))
        if not data.startswith("rec:"):
            return
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        _prefix, request_id, item_id = parts
        # Publish a recordings.get command correlated with the original request
        await self._publish_command(
            ControlCommand(
                command="recordings.get",
                camera_id=None,
                arguments={"request_id": request_id, "item_id": item_id},
            )
        )

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

    async def _ensure_superuser(self, update: Any, context: Any) -> bool:
        """Ensure the caller passes general checks and is the configured superuser."""
        if not await self._ensure_command_allowed(update, context):
            return False
        user_id = self._user_id(update)
        if user_id != self._superuser_id:
            if not self._silent_unauthorized:
                await self._respond(update, "Superuser privileges are required for this command.")
            return False
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
