"""
Telegram Bot Interface - Event-based notifications with recipe configuration.

Handles Telegram notifications based on recipe settings.
Sends images/videos with overlays when events are detected.
"""

import asyncio
import html
import io
import queue
import time
from contextlib import suppress
from collections import deque
import logging
import re
from types import SimpleNamespace
from functools import wraps
import cv2
import imageio
import numpy as np
import yaml
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Set, Tuple
from dataclasses import dataclass

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut

from .api_client import SpyoncinoHttpClient
from .authz import Principal, auth_state_from_config, can
from .memory_manager import MemoryManager, EventType

from ..recipe_classes import normalize_notify_modes

# Media list callback payload meaning "no camera/stage filter" (must match keyboard rows).
_MEDIA_LIST_ALL = "".join(chr(c) for c in (97, 108, 108))

# Outbound queue: alerts wait when Telegram rate limit is active (no drop-on-enqueue).
_NOTIFICATION_QUEUE_MAX = 512
# Max items pulled from the queue in one drain pass (then text-merge + per-GIF sends).
_NOTIFICATION_DRAIN_BATCH_MAX = 48
# Emergency: while rate-limited, if backlog is this large, send one recap and clear (no GIF burst).
_EMERGENCY_RECAP_THRESHOLD = 32
# Batch strategy: if pending ≥ this, send digest chunk(s) instead of per-alert GIF until caught up.
_BATCH_DIGEST_THRESHOLD = 10
_BATCH_DIGEST_CHUNK = 15

# SQLite ``config`` keys documented for Telegram; orchestrator/bot read these when set.
_SQLITE_CONFIG_KNOBS: Tuple[Tuple[str, str, str], ...] = (
    (
        "patrol_time",
        "float",
        "Seconds between patrol cycles (0.2–3600). Omit key to use recipe.",
    ),
    (
        "notification_rate_limit",
        "int",
        "Max outbound Telegram messages per rolling 60s (≥1). Large backlog while limited is cleared with a notice.",
    ),
    (
        "notify_on_preproc",
        "list",
        'Motion alerts: subset of text, gif, video — e.g. ["text"]',
    ),
    (
        "notify_on_detection",
        "list",
        'Person alerts: e.g. ["gif","text"]',
    ),
)


def _require_policy_action(
    action: str,
    *,
    deny_via_unauthorized_response: bool = True,
    deny_text: Optional[str] = None,
    enforce_group_scope: bool = True,
):
    """Decorator factory for Telegram command handlers guarded by authz action."""

    def _decorate(func):
        @wraps(func)
        async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            if not user:
                if update.message:
                    await update.message.reply_text("❌ Unable to identify user.")
                return
            if enforce_group_scope:
                if self._is_group_context(update) and not self._allow_group_commands:
                    return
                if self._is_group_context(
                    update
                ) and not self._is_allowed_group_command_chat(update):
                    return
            if self._can_telegram(user.id, action):
                return await func(self, update, context)
            if deny_via_unauthorized_response:
                await self._unauthorized_response(update)
                return
            if deny_text and update.message:
                await update.message.reply_text(deny_text)
            return

        return wrapper

    return _decorate


def _require_authorization(func):
    """Decorator for command handlers requiring authorized user access."""
    return _require_policy_action("view_status")(func)


def _require_superuser(func):
    """Decorator for command handlers requiring superuser access."""
    return _require_policy_action(
        "manage_whitelist",
        deny_via_unauthorized_response=False,
        deny_text="🚫 Superuser access required.",
        enforce_group_scope=False,
    )(func)


@dataclass
class NotificationEvent:
    """Represents a notification event."""

    message: str
    event_type: str
    stage: str
    camera_id: Optional[str] = None
    frames: Optional[List] = None
    overlays: Optional[List] = None
    timestamp: datetime = None
    prefer_plain_text: bool = False
    photo_path: Optional[str] = None
    reply_markup: Optional[InlineKeyboardMarkup] = None
    # When False, photo caption is exactly ``message`` (already includes any timestamp).
    append_timestamp_footer: bool = True
    # Set when the event enters the outbound queue (for backlog / batch heuristics).
    enqueued_at: Optional[datetime] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class TelegramBotInterface:
    """
    Telegram bot interface for event-based notifications.

    Outbound (see recipe ``params.config``):
    - ``outbound_strategy``: ``normal`` (default) or ``batch``. Batch sends text digest
      chunk(s) when backlog ≥ threshold so GIF/video do not pile up.
    - ``notification_rate_limit``: max Telegram messages per rolling 60s.

    When rate-limited with a large backlog, **emergency recap** sends one digest and clears the queue.
    """

    def __init__(
        self,
        secrets_path: str,
        memory_manager: Optional[MemoryManager] = None,
        config: Optional[Dict[str, Any]] = None,
        media_store: Optional[Any] = None,  # MediaStore when provided by orchestrator
    ):
        """
        Initialize Telegram bot interface.

        Args:
            secrets_path: Path to secrets.yaml file containing Telegram credentials
            memory_manager: MemoryManager instance
            config: Configuration dictionary from recipe
            media_store: Optional MediaStore for persisted recordings (path + DB index)
        """
        # Load secrets from YAML file
        secrets_file = Path(secrets_path)
        if not secrets_file.exists():
            raise FileNotFoundError(f"Secrets file not found: {secrets_path}")

        with open(secrets_file, "r") as f:
            secrets = yaml.safe_load(f)
        secrets = secrets if isinstance(secrets, dict) else {}
        self._secrets_path = secrets_file
        self._secrets_data = secrets

        telegram_secrets = secrets.get("telegram", {})
        if not telegram_secrets:
            raise ValueError(
                f"No 'telegram' section found in secrets file: {secrets_path}"
            )

        self.token = telegram_secrets.get("token")
        if not self.token:
            raise ValueError(
                f"No 'token' found in telegram section of secrets file: {secrets_path}"
            )

        self.chat_id = telegram_secrets.get("chat_id")
        # Optional dynamic notification destination (single bound group).
        self.notification_chat_id = telegram_secrets.get("notification_chat_id")
        # Backward compatibility for old key naming.
        if self.notification_chat_id is None:
            self.notification_chat_id = telegram_secrets.get("group_chat_id")

        # Store authentication settings for potential future use
        self.auth_config = secrets.get("authentication", {})
        if not isinstance(self.auth_config, dict):
            self.auth_config = {}
        self._superuser_id = self.auth_config.get("superuser_id")
        if isinstance(self._superuser_id, str) and self._superuser_id.isdigit():
            self._superuser_id = int(self._superuser_id)
        if not isinstance(self._superuser_id, int):
            self._superuser_id = None
        whitelist_raw = self.auth_config.get("user_whitelist", [])
        self._user_whitelist: List[int] = []
        if isinstance(whitelist_raw, list):
            for v in whitelist_raw:
                if isinstance(v, int):
                    self._user_whitelist.append(v)
                elif isinstance(v, str) and v.isdigit():
                    self._user_whitelist.append(int(v))
        self._setup_password = self.auth_config.get("setup_password")
        if (
            not isinstance(self._setup_password, str)
            or not self._setup_password.strip()
        ):
            self._setup_password = None
        self._allow_group_commands = bool(
            self.auth_config.get("allow_group_commands", True)
        )
        self._silent_unauthorized = bool(
            self.auth_config.get("silent_unauthorized", True)
        )
        self._failed_attempts: Dict[int, int] = {}
        self._auth_state = auth_state_from_config(self.auth_config)

        self.memory_manager = memory_manager
        self.media_store = media_store
        self.config = config or {}

        _api_sec = secrets.get("spyoncino_api")
        api_from_secrets = _api_sec if isinstance(_api_sec, dict) else {}
        api_base = (
            self.config.get("api_base_url") or api_from_secrets.get("base_url") or ""
        ).strip()
        api_key = api_from_secrets.get("api_key") or self.config.get("api_key")
        api_key = (
            api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        )
        self._http_api: Optional[SpyoncinoHttpClient] = None
        if api_base:
            self._http_api = SpyoncinoHttpClient(api_base, api_key=api_key)

        # Notification settings from recipe (per pipeline stage)
        self.notify_preproc = normalize_notify_modes(
            self.config.get("notify_on_preproc")
        )
        self.notify_detection = normalize_notify_modes(
            self.config.get("notify_on_detection")
        )
        # Face stage uses structured messages (text/photo) from the face pipeline; recipe
        # does not need notify_on_face. Default GIF fallback matches previous recipe default.
        if "notify_on_face" in (self.config or {}):
            self.notify_face = normalize_notify_modes(self.config.get("notify_on_face"))
        else:
            self.notify_face = normalize_notify_modes(["gif"])

        gif_cfg = self.config.get("gif") or {}
        video_cfg = self.config.get("video") or {}
        self.gif_fps = int(gif_cfg.get("fps", 10))
        self.gif_duration = float(gif_cfg.get("duration", 3))
        self.video_fps = int(video_cfg.get("fps", 10))
        self.video_duration = float(video_cfg.get("duration", 3))
        self.video_format = str(video_cfg.get("format", "mp4")).lower().strip()

        self.max_file_size_mb = self.config.get("max_file_size_mb", 50.0)
        self.notification_rate_limit = self.config.get("notification_rate_limit", 5)
        # ``normal``: queue + drain (text merge); GIF per alert when possible.
        # ``batch``: when backlog ≥ threshold, send text digest chunk(s) instead of GIF until caught up.
        _os = str(self.config.get("outbound_strategy", "normal")).lower().strip()
        self._outbound_strategy: str = (
            "batch" if _os in ("batch", "batched", "summary") else "normal"
        )

        # Pause reminder (job_queue): accumulate seconds while paused, notify every max(120, 100*patrol_time)s
        self._pause_reminder_was_paused = False
        self._pause_reminder_accumulator_s = 0.0

        # Callback prefix for inline menus (keep payloads short; Telegram limit 64 bytes)
        self._cb = "sc"

        # Telegram application
        self.app = Application.builder().token(self.token).build()

        # Notification queue (alerts wait when rate-limited instead of being dropped)
        self.notification_queue: queue.Queue = queue.Queue(
            maxsize=_NOTIFICATION_QUEUE_MAX
        )
        self.notification_stats = {
            "sent": 0,
            "failed": 0,
            "queue_dropped": 0,
            "emergency_recaps": 0,
            "batch_summaries": 0,
            "backlog_alerts_cleared": 0,
        }

        # Rate limiting (rolling 60s window of outbound Telegram messages)
        self._notification_times: List[datetime] = []
        # Telegram API 429 RetryAfter: monotonic time until we may send again (stricter than local cap).
        self._telegram_flood_until: Optional[float] = None
        # Alerts left when a drain pass hits the limit mid-batch (FIFO preserved).
        self._requeue_front: deque = deque()
        # Short ref (8 hex, lower) -> full pending_face UUID for inline buttons and /fa.
        self._face_pending_by_ref: Dict[str, str] = {}
        # Bound to the bot event loop in start() — serializes queue drains across slow Telegram I/O.
        self._notification_drain_lock: Optional[asyncio.Lock] = None
        self._notification_pump_task: Optional[asyncio.Task] = None

        # Setup command handlers
        self._setup_handlers()

        # Logging
        self.logger = logging.getLogger(self.__class__.__name__)
        if self._http_api:
            self.logger.info("Telegram commands will use HTTP API at %s", api_base)

        # Log initialization
        if self.memory_manager:
            self.memory_manager.log_event(
                EventType.STARTUP, "Telegram bot interface initialized", severity="info"
            )

    def _setup_handlers(self) -> None:
        """Setup command handlers."""
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("setup", self.cmd_setup))
        self.app.add_handler(CommandHandler("whoami", self.cmd_whoami))
        self.app.add_handler(CommandHandler("bind_group", self.cmd_bind_group))
        self.app.add_handler(CommandHandler("unbind_group", self.cmd_unbind_group))
        self.app.add_handler(CommandHandler("whitelist_add", self.cmd_whitelist_add))
        self.app.add_handler(
            CommandHandler("whitelist_remove", self.cmd_whitelist_remove)
        )
        self.app.add_handler(CommandHandler("whitelist_list", self.cmd_whitelist_list))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("whosthere", self.cmd_whosthere))
        self.app.add_handler(CommandHandler("media", self.cmd_media))
        self.app.add_handler(CommandHandler("show_config", self.cmd_show_config))
        self.app.add_handler(CommandHandler("config", self.cmd_config))
        self.app.add_handler(CommandHandler("config_reset", self.cmd_config_reset))
        self.app.add_handler(CommandHandler("stats", self.cmd_stats))
        self.app.add_handler(CommandHandler("cams", self.cmd_cams))
        self.app.add_handler(CommandHandler("pause", self.cmd_pause))
        self.app.add_handler(CommandHandler("snap", self.cmd_snap))
        self.app.add_handler(CommandHandler("test", self.cmd_test))
        self.app.add_handler(
            CallbackQueryHandler(self._on_menu_callback, pattern=r"^sc\|")
        )
        self.app.add_handler(CommandHandler("face_assign", self.cmd_face_assign))
        self.app.add_handler(CommandHandler("fa", self.cmd_face_assign))
        self.app.add_handler(CommandHandler("face_ignore", self.cmd_face_ignore))

    def _cb_data(self, *segments: str) -> str:
        return "|".join((self._cb,) + tuple(segments))

    def _register_face_pending_ref(self, pending_id: str) -> Tuple[str, str]:
        """
        Remember pending id under an 8-hex ref for callbacks and /fa.
        Returns (ref_lower, ref_display_upper).
        """
        hx = pending_id.replace("-", "")[:8].lower()
        if len(hx) != 8 or not re.match(r"^[0-9a-f]{8}$", hx):
            return "", ""
        self._face_pending_by_ref[hx] = pending_id
        while len(self._face_pending_by_ref) > 128:
            self._face_pending_by_ref.pop(next(iter(self._face_pending_by_ref)))
        return hx, hx.upper()

    def _expand_pending_token(self, token: str) -> Optional[str]:
        """Resolve 8-char ref from a recent alert, or compact/full UUID string."""
        t = (token or "").strip()
        tl = t.lower()
        if len(tl) == 8 and re.match(r"^[0-9a-f]{8}$", tl):
            hit = self._face_pending_by_ref.get(tl)
            if hit:
                return hit
        return self._uuid_from_compact(t)

    def _resolve_identity_hex_prefix(self, prefix: str) -> Optional[str]:
        """Match SQLite identity id by leading hex digits (no dashes)."""
        if not self.memory_manager or not prefix:
            return None
        pfx = prefix.strip().lower()
        if not re.match(r"^[0-9a-f]+$", pfx):
            return None
        hits: List[str] = []
        try:
            rows = self.memory_manager.list_identities()
        except Exception:
            return None
        for row in rows:
            iid = str(row.get("id") or "")
            h = iid.replace("-", "").lower()
            if h.startswith(pfx):
                hits.append(iid)
        if len(hits) == 1:
            return hits[0]
        return None

    def _unknown_face_reply_markup(
        self, ref_lower: str
    ) -> Optional[InlineKeyboardMarkup]:
        """Ignore, assign-to-known (one row per identity, capped), or new-name hint."""
        if not self._http_api or len(ref_lower) != 8:
            return None
        rows: List[List[InlineKeyboardButton]] = []
        ig = self._cb_data("ig", ref_lower)
        if len(ig.encode("utf-8")) > 64:
            return None
        rows.append([InlineKeyboardButton("Ignore", callback_data=ig)])
        max_id_buttons = 18
        identities: List[Dict[str, Any]] = []
        if self.memory_manager:
            try:
                identities = list(self.memory_manager.list_identities())
            except Exception:
                identities = []
        extra = 0
        if len(identities) > max_id_buttons:
            extra = len(identities) - max_id_buttons
            identities = identities[:max_id_buttons]
        for row in identities:
            label = str(row.get("display_name") or "?").strip() or "?"
            if len(label) > 28:
                label = label[:25] + "…"
            iid = str(row.get("id") or "")
            hx = iid.replace("-", "")[:12].lower()
            if len(hx) < 8:
                continue
            cb = self._cb_data("as", ref_lower, hx)
            if len(cb.encode("utf-8")) > 64:
                continue
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
        nw = self._cb_data("nw", ref_lower)
        if len(nw.encode("utf-8")) <= 64:
            hint = "+ New name…"
            if extra:
                hint = f"+ New… ({extra} ids not shown)"
            rows.append([InlineKeyboardButton(hint, callback_data=nw)])
        return InlineKeyboardMarkup(rows)

    def _main_menu_keyboard(self) -> Optional[InlineKeyboardMarkup]:
        """Quick actions when HTTP API is configured."""
        if not self._http_api:
            return None
        rows = [
            [
                InlineKeyboardButton("📊 Status", callback_data=self._cb_data("st")),
                InlineKeyboardButton(
                    "⏸ Pause / ▶ Resume", callback_data=self._cb_data("ps")
                ),
            ],
            [
                InlineKeyboardButton("🗂 Media", callback_data=self._cb_data("md")),
                InlineKeyboardButton("📷 Snap…", callback_data=self._cb_data("sn")),
            ],
            [
                InlineKeyboardButton(
                    "📈 Stats (24h)", callback_data=self._cb_data("ss")
                ),
                InlineKeyboardButton("❓ Help", callback_data=self._cb_data("hp")),
            ],
        ]
        return InlineKeyboardMarkup(rows)

    def _snap_camera_keyboard(self, camera_ids: List[str]) -> InlineKeyboardMarkup:
        row: List[InlineKeyboardButton] = []
        rows: List[List[InlineKeyboardButton]] = []
        for cid in camera_ids:
            payload = self._cb_data("sn", cid)
            if len(payload.encode("utf-8")) > 64:
                continue
            row.append(InlineKeyboardButton(f"📷 {cid}", callback_data=payload))
            if len(row) >= 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return InlineKeyboardMarkup(rows)

    def _media_camera_keyboard(self, camera_ids: List[str]) -> InlineKeyboardMarkup:
        rows: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        all_payload = self._cb_data("mc", _MEDIA_LIST_ALL)
        if len(all_payload.encode("utf-8")) <= 64:
            row.append(
                InlineKeyboardButton("🌐 All cameras", callback_data=all_payload)
            )
        for cid in camera_ids:
            payload = self._cb_data("mc", cid)
            if len(payload.encode("utf-8")) > 64:
                continue
            row.append(InlineKeyboardButton(f"🎥 {cid}", callback_data=payload))
            if len(row) >= 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return InlineKeyboardMarkup(rows)

    def _media_stage_keyboard(self) -> InlineKeyboardMarkup:
        stages = [
            ("📦 Any stage", _MEDIA_LIST_ALL),
            ("🚨 detection", "detection"),
            ("👀 preproc", "preproc"),
            ("📷 snap", "snap"),
            ("🙂 face", "face"),
        ]
        rows: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        for label, st in stages:
            payload = self._cb_data("ms", st)
            if len(payload.encode("utf-8")) > 64:
                continue
            row.append(InlineKeyboardButton(label, callback_data=payload))
            if len(row) >= 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return InlineKeyboardMarkup(rows)

    async def cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /start command."""
        if (
            not self.chat_id
            and update.effective_chat
            and update.effective_chat.type == "private"
        ):
            self.chat_id = update.effective_chat.id
            self._save_telegram_config()
            self.logger.info(f"Chat ID set to: {self.chat_id}")

        api_note = (
            "\n<i>Commands use the Spyoncino HTTP API.</i>"
            if self._http_api
            else "\n<i>Set <code>api_base_url</code> in the recipe for control commands.</i>"
        )
        text = (
            "<b>Spyoncino</b> — security bot\n\n"
            "You will receive alerts when the pipeline detects motion or objects.\n"
            f"{api_note}\n\n"
            "<b>Quick start</b>\n"
            "• <code>/whoami</code> — your Telegram id and access\n"
            "• <code>/setup</code> — first-time superuser (if not configured)\n"
            "• <code>/status</code> — patrol, metrics, services\n"
            "• <code>/whosthere [h]</code> — recently recognized people (default last 1h)\n"
            "• <code>/stats [h]</code> — analytics window + trend chart\n"
            "• <code>/help</code> — full command list\n\n"
            "<b>Inline menu</b> — use the buttons below when the API is available."
        )
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=self._main_menu_keyboard(),
        )

    async def cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /help — same command reference as /start, without chat-id side effects."""
        api_note = "\n<i>HTTP API:</i> " + (
            "enabled" if self._http_api else "set <code>api_base_url</code> to enable"
        )
        text = (
            "<b>Commands</b>\n"
            "<code>/status</code> — patrol, cycles, services, live counters\n"
            "<code>/whosthere [hours]</code> — who was recognized recently (default 1)\n"
            "<code>/stats [hours]</code> — analytics window + trend chart (default 24)\n"
            "<code>/pause</code> — toggle pause / resume (same as inline button)\n"
            "<code>/snap</code> — choose camera, or <code>/snap &lt;camera_id&gt;</code>\n"
            "<code>/media</code> — filter then grid of tap-to-open items, or <code>/media [camera] [stage]</code>\n"
            "<code>/cams</code> — camera ids from the running recipe\n"
            "<code>/show_config</code> — tunable keys, stored + live values\n"
            "<code>/config</code> alone = same; <code>/config key value</code> = set\n"
            "<code>/config_reset key</code> or <code>/config_reset all</code> — remove DB override(s)\n"
            "<code>/whoami</code> <code>/setup</code> — identity / bootstrap\n"
            "<code>/bind_group</code> <code>/unbind_group</code> — superuser, notification target\n"
            "<code>/whitelist_add|remove|list</code> — superuser\n"
            "<code>/test</code> — enqueue a test notification\n"
            "<code>/face_assign</code> <code>/fa</code> <code>/face_ignore</code> — unknown faces (ref from alert; needs API)\n"
            f"{api_note}"
        )
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=self._main_menu_keyboard(),
        )

    def _is_group_context(self, update: Update) -> bool:
        chat = update.effective_chat
        return bool(chat and chat.type in ("group", "supergroup"))

    def _is_superuser(self, user_id: int) -> bool:
        return bool(self._superuser_id and user_id == self._superuser_id)

    def _notification_target_chat_id(self) -> Optional[int]:
        return self.notification_chat_id or self.chat_id

    def _is_allowed_group_command_chat(self, update: Update) -> bool:
        chat = update.effective_chat
        if not chat or chat.type not in ("group", "supergroup"):
            return True
        if self.notification_chat_id is None:
            return False
        return chat.id == self.notification_chat_id

    def _can_telegram(self, user_id: int, action: str) -> bool:
        return can(
            Principal(kind="telegram", user_id=user_id), action, self._auth_state
        )

    def _is_authorized_user(self, user_id: int) -> bool:
        if self._is_rate_limited_user(user_id):
            return False
        return self._can_telegram(user_id, "view_status")

    def _is_rate_limited_user(self, user_id: int) -> bool:
        return self._failed_attempts.get(user_id, 0) >= 5

    def _record_failed_attempt(self, user_id: int) -> None:
        self._failed_attempts[user_id] = self._failed_attempts.get(user_id, 0) + 1

    def _reset_failed_attempts(self, user_id: int) -> None:
        self._failed_attempts.pop(user_id, None)

    def _save_auth_config(self) -> None:
        auth = self._secrets_data.get("authentication")
        if not isinstance(auth, dict):
            auth = {}
            self._secrets_data["authentication"] = auth
        if self._setup_password:
            auth["setup_password"] = self._setup_password
        auth["superuser_id"] = self._superuser_id
        auth["user_whitelist"] = sorted(set(self._user_whitelist))
        auth["allow_group_commands"] = self._allow_group_commands
        auth["silent_unauthorized"] = self._silent_unauthorized
        with open(self._secrets_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._secrets_data, f, sort_keys=False)
        self._auth_state = auth_state_from_config(auth)

    def _save_telegram_config(self) -> None:
        telegram = self._secrets_data.get("telegram")
        if not isinstance(telegram, dict):
            telegram = {}
            self._secrets_data["telegram"] = telegram
        telegram["chat_id"] = self.chat_id
        telegram["notification_chat_id"] = self.notification_chat_id
        # Drop legacy key if present to avoid split-brain config.
        telegram.pop("group_chat_id", None)
        with open(self._secrets_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._secrets_data, f, sort_keys=False)

    async def _unauthorized_response(self, update: Update) -> None:
        if not update.message:
            return
        user = update.effective_user
        if not user:
            await update.message.reply_text("🚫 Unauthorized.")
            return
        self._record_failed_attempt(user.id)
        if self._is_group_context(update) and self._silent_unauthorized:
            return
        if self._is_rate_limited_user(user.id):
            await update.message.reply_text(
                "🚫 Too many failed attempts. Access temporarily blocked."
            )
            return
        await update.message.reply_text(f"🚫 Unauthorized access — user id: {user.id}")

    async def cmd_setup(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Bootstrap first superuser with setup password."""
        if not update.message or not update.effective_user:
            return
        user_id = update.effective_user.id
        if self._superuser_id:
            await update.message.reply_text("🔒 System already configured.")
            return
        if self._is_rate_limited_user(user_id):
            await update.message.reply_text(
                "🚫 Too many failed attempts. Try again later."
            )
            return
        if not self._setup_password:
            self._superuser_id = user_id
            self._user_whitelist = [user_id]
            self._reset_failed_attempts(user_id)
            self._save_auth_config()
            await update.message.reply_text("✅ Setup complete. You are now superuser.")
            return
        args = (context.args or []) if context else []
        if not args:
            await update.message.reply_text("Usage: /setup <password>")
            return
        password = " ".join(args).strip()
        if password != self._setup_password:
            self._record_failed_attempt(user_id)
            attempts_left = max(0, 5 - self._failed_attempts.get(user_id, 0))
            await update.message.reply_text(
                f"❌ Invalid setup password. Attempts left: {attempts_left}"
            )
            return
        self._superuser_id = user_id
        self._user_whitelist = [user_id]
        self._reset_failed_attempts(user_id)
        self._save_auth_config()
        await update.message.reply_text("✅ Setup complete. You are now superuser.")

    async def cmd_whoami(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show user identity and authorization status."""
        if not update.message or not update.effective_user:
            return
        user = update.effective_user
        if self._is_superuser(user.id):
            status = "👑 Superuser"
        elif self._is_authorized_user(user.id):
            status = "✅ Authorized"
        else:
            status = "🚫 Unauthorized"
        await update.message.reply_text(
            f"👤 <b>User</b>\n"
            f"• id: <code>{user.id}</code>\n"
            f"• username: @{user.username or 'none'}\n"
            f"• status: {status}\n"
            f"• active notification chat: <code>{self._notification_target_chat_id()}</code>",
            parse_mode="HTML",
        )

    @_require_superuser
    async def cmd_bind_group(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Bind current group chat as notification destination."""
        if not update.message or not update.effective_chat:
            return
        chat = update.effective_chat
        if chat.type not in ("group", "supergroup"):
            await update.message.reply_text(
                "Usage: run /bind_group inside a group chat."
            )
            return
        self.notification_chat_id = chat.id
        self._save_telegram_config()
        await update.message.reply_text(
            f"✅ Bound this group for notifications.\n"
            f"notification_chat_id=<code>{self.notification_chat_id}</code>",
            parse_mode="HTML",
        )

    @_require_superuser
    async def cmd_unbind_group(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Unbind current group chat from notification destination."""
        if not update.message or not update.effective_chat:
            return
        chat = update.effective_chat
        if chat.type not in ("group", "supergroup"):
            await update.message.reply_text(
                "Run /unbind_group inside the currently bound group."
            )
            return
        if self.notification_chat_id is None:
            await update.message.reply_text("ℹ️ No group is currently bound.")
            return
        if chat.id != self.notification_chat_id:
            await update.message.reply_text("❌ This is not the currently bound group.")
            return
        self.notification_chat_id = None
        self._save_telegram_config()
        await update.message.reply_text(
            f"✅ Group unbound. Notifications now fallback to superuser chat.\n"
            f"chat_id=<code>{self.chat_id}</code>",
            parse_mode="HTML",
        )

    @_require_superuser
    async def cmd_whitelist_add(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return
        args = (context.args or []) if context else []
        if len(args) != 1:
            await update.message.reply_text("Usage: /whitelist_add <user_id>")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid user id.")
            return
        if user_id <= 0:
            await update.message.reply_text("❌ User id must be positive.")
            return
        if user_id in self._user_whitelist:
            await update.message.reply_text("ℹ️ User already whitelisted.")
            return
        self._user_whitelist.append(user_id)
        self._save_auth_config()
        await update.message.reply_text(f"✅ User {user_id} added to whitelist.")

    @_require_superuser
    async def cmd_whitelist_remove(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return
        args = (context.args or []) if context else []
        if len(args) != 1:
            await update.message.reply_text("Usage: /whitelist_remove <user_id>")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid user id.")
            return
        if user_id == self._superuser_id:
            await update.message.reply_text(
                "❌ Cannot remove superuser from whitelist."
            )
            return
        if user_id not in self._user_whitelist:
            await update.message.reply_text("ℹ️ User not in whitelist.")
            return
        self._user_whitelist.remove(user_id)
        self._save_auth_config()
        await update.message.reply_text(f"✅ User {user_id} removed from whitelist.")

    @_require_superuser
    async def cmd_whitelist_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return
        if not self._user_whitelist:
            await update.message.reply_text("📝 Whitelist is empty (open access).")
            return
        users = "\n".join(f"• {uid}" for uid in sorted(set(self._user_whitelist)))
        await update.message.reply_text(
            f"📝 <b>Whitelisted users</b>\n{users}\n\n"
            f"👑 Superuser: <code>{self._superuser_id}</code>",
            parse_mode="HTML",
        )

    def _format_status_html(self, data: Dict[str, Any]) -> str:
        metrics = data.get("metrics") or {}
        uptime_s = float(metrics.get("uptime_seconds") or 0)
        uptime_hours = int(uptime_s // 3600)
        uptime_minutes = int((uptime_s % 3600) // 60)
        paused = bool(data.get("paused"))
        cycles = data.get("total_cycles")
        patrol = data.get("patrol_time")
        services = metrics.get("services") or {}
        cams = data.get("camera_ids") or []

        status_text = (
            f"📊 <b>System status</b> <i>(API)</i>\n\n"
            f"⏸ Patrol: <b>{'paused' if paused else 'running'}</b>\n"
            f"🔁 Cycles: {cycles}\n"
            f"⏱ Patrol interval: {patrol}s\n"
            f"⏱ Uptime: {uptime_hours}h {uptime_minutes}m\n"
            f"📈 Total events: {metrics.get('total_events', 0)}\n"
            f"👀 Motion: {metrics.get('motion_events', 0)}\n"
            f"🚨 Person: {metrics.get('person_events', 0)}\n"
            f"🙂 Face: {metrics.get('face_events', 0)}\n"
            f"❌ Errors (lifetime counter): {metrics.get('error_events', 0)}\n"
        )
        if cams:
            status_text += f"🎥 Cameras: {', '.join(str(c) for c in cams)}\n"
        status_text += "\n"
        to = data.get("telegram_outbound")
        if isinstance(to, dict):
            status_text += (
                "<b>Telegram outbound</b>\n"
                f"⚙️ Strategy: <code>{to.get('outbound_strategy', '?')}</code> "
                f"(<code>normal</code> or <code>batch</code>)\n"
                f"📥 Queue depth: <code>{to.get('queue_depth', '?')}</code>"
                f" · requeue: <code>{to.get('requeue_depth', '?')}</code>\n"
                f"📤 Sent (session): <code>{to.get('notifications_sent', 0)}</code>\n"
                f"❌ Failed: <code>{to.get('notifications_failed', 0)}</code>\n"
                f"🚨 Emergency recaps: <code>{to.get('notifications_emergency_recaps', to.get('notifications_clog_clears', 0))}</code>"
                f" · batch summaries: <code>{to.get('notifications_batch_summaries', 0)}</code>\n"
                f"📭 Queue full drops: <code>{to.get('notifications_queue_dropped', 0)}</code>"
                f" · backlog cleared (emergency): <code>{to.get('notifications_backlog_alerts_cleared', 0)}</code>\n"
                f"🧱 Max/60s (rolling): <code>{to.get('rate_limit_per_minute', '?')}</code>\n\n"
            )
        else:
            status_text += (
                f"📬 Notifications sent (bot): {self.notification_stats['sent']}\n"
                f"❌ Failed (bot): {self.notification_stats['failed']}\n\n"
            )
        status_text += "<b>Services</b>\n"
        for name, st in services.items():
            if isinstance(st, dict):
                running = st.get("is_running", False)
            else:
                running = getattr(st, "is_running", False)
            status_icon = "✅" if running else "❌"
            status_text += f"{status_icon} {name}\n"
        return status_text

    @_require_authorization
    async def cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /status via HTTP API — runtime snapshot only (use /stats for analytics chart)."""
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> (and optional API key) to use /status.",
                parse_mode="HTML",
            )
            return

        user_id = update.effective_user.id if update.effective_user else None
        try:
            data = await self._http_api.get_status(user_id=user_id)
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await update.message.reply_text(
                f"❌ API error ({e.response.status_code}): {detail}"
            )
            return
        except httpx.RequestError as e:
            await update.message.reply_text(
                f"❌ Cannot reach Spyoncino API (is the web server up?).\n{e!s}"
            )
            return

        status_text = self._format_status_html(data)
        await update.message.reply_text(
            status_text,
            parse_mode="HTML",
            reply_markup=self._main_menu_keyboard(),
        )

    @staticmethod
    def _format_whosthere_html(data: Dict[str, Any]) -> str:
        h = int(data.get("hours") or 1)
        identified = data.get("identified") or []
        unknowns = data.get("unknown_glimpses") or []
        lines: List[str] = [f"🙂 <b>Who's there</b> <i>(last {h}h)</i>\n"]
        if identified:
            lines.append("<b>Recognized</b>")
            for row in identified[:35]:
                if not isinstance(row, dict):
                    continue
                nm = html.escape(str(row.get("display_name") or "?").strip() or "?")
                cam = html.escape(str(row.get("camera_id") or "—"))
                ts = html.escape(str(row.get("last_seen") or ""))
                lines.append(f"• <b>{nm}</b> · <code>{cam}</code> · <i>{ts}</i>")
        else:
            lines.append("<i>No named recognitions in this window.</i>")
        if not identified and not unknowns:
            lines.append(
                "<i>Face names are attached to alerts from current builds.</i>"
            )
        if unknowns:
            lines.append("")
            lines.append("<b>Unknown face alerts</b> <i>(not in gallery)</i>")
            for row in unknowns[:12]:
                if not isinstance(row, dict):
                    continue
                cam = html.escape(str(row.get("camera_id") or "—"))
                ts = html.escape(str(row.get("last_seen") or ""))
                cnt = int(row.get("count") or 0)
                lines.append(f"• <code>{cam}</code> ×{cnt} · <i>{ts}</i>")
        return "\n".join(lines)

    @_require_authorization
    async def cmd_whosthere(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List people recognized recently (from FACE event log)."""
        if not update.message:
            return
        args = (context.args or []) if context else []
        hours = 1
        if args:
            try:
                hours = max(1, min(168, int(args[0])))
            except ValueError:
                await update.message.reply_text(
                    "Usage: <code>/whosthere [hours]</code> — hours 1–168 (default 1).",
                    parse_mode="HTML",
                )
                return
        user_id = update.effective_user.id if update.effective_user else None
        try:
            if self._http_api:
                data = await self._http_api.get_recent_face_presence(
                    hours=hours, user_id=user_id
                )
            elif self.memory_manager:
                data = self.memory_manager.recent_identified_presence(hours=hours)
            else:
                await update.message.reply_text(
                    "❌ Need <code>api_base_url</code> or an in-process <code>memory_manager</code>.",
                    parse_mode="HTML",
                )
                return
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await update.message.reply_text(
                f"❌ API error ({e.response.status_code}): {detail}"
            )
            return
        except httpx.RequestError as e:
            await update.message.reply_text(f"❌ API unreachable: {e!s}")
            return
        txt = self._format_whosthere_html(data)
        await update.message.reply_text(
            txt,
            parse_mode="HTML",
            reply_markup=self._main_menu_keyboard(),
        )

    @_require_authorization
    async def cmd_cams(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List camera ids from /api/status."""
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> to use /cams.",
                parse_mode="HTML",
            )
            return
        user_id = update.effective_user.id if update.effective_user else None
        try:
            data = await self._http_api.get_status(user_id=user_id)
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await update.message.reply_text(f"❌ API error: {detail}")
            return
        except httpx.RequestError as e:
            await update.message.reply_text(f"❌ API unreachable: {e!s}")
            return
        cams = data.get("camera_ids") or []
        if not cams:
            await update.message.reply_text(
                "🎥 No <code>camera_ids</code> reported by API.", parse_mode="HTML"
            )
            return
        body = "\n".join(f"• <code>{c}</code>" for c in cams)
        await update.message.reply_text(
            f"🎥 <b>Cameras</b>\n{body}",
            parse_mode="HTML",
            reply_markup=self._snap_camera_keyboard(cams),
        )

    @_require_authorization
    async def cmd_pause(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Toggle patrol pause via HTTP API (paused → resume, running → pause). Extra args ignored."""
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> (and optional API key) to use /pause.",
                parse_mode="HTML",
            )
            return
        user_id = update.effective_user.id if update.effective_user else None
        try:
            st = await self._http_api.get_status(user_id=user_id)
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await update.message.reply_text(f"❌ API error: {detail}")
            return
        except httpx.RequestError as e:
            await update.message.reply_text(f"❌ API unreachable: {e!s}")
            return
        paused = not bool(st.get("paused"))
        try:
            out = await self._http_api.set_paused(paused, user_id=user_id)
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await update.message.reply_text(f"❌ API error: {detail}")
            return
        except httpx.RequestError as e:
            await update.message.reply_text(f"❌ API unreachable: {e!s}")
            return
        is_paused = bool(out.get("paused"))
        self._pause_reminder_was_paused = False
        self._pause_reminder_accumulator_s = 0.0
        await update.message.reply_text(
            f"⏸ Patrol is now <b>{'paused' if is_paused else 'running'}</b>.",
            parse_mode="HTML",
            reply_markup=self._main_menu_keyboard(),
        )

    async def _send_media_list(
        self,
        message,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        camera_id: Optional[str],
        stage: Optional[str],
        user_id: Optional[int],
    ) -> None:
        try:
            rows = await self._http_api.list_media(
                camera_id=camera_id,
                stage=stage,
                hours=24 * 7,
                limit=40,
                offset=0,
                user_id=user_id,
            )
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await message.reply_text(f"❌ API error: {detail}")
            return
        except httpx.RequestError as e:
            await message.reply_text(f"❌ API unreachable: {e!s}")
            return

        cam_lbl = camera_id or "all"
        st_lbl = stage or "any"
        if not rows:
            await message.reply_text(
                f"📭 <b>Media</b>\n<i>{cam_lbl}</i> · <i>{st_lbl}</i>\nNo artifacts match.",
                parse_mode="HTML",
            )
            return

        header = (
            f"🗂 <b>Recent media</b> — <i>newest {len(rows)}</i>, tap a button to open\n"
            f"<code>{cam_lbl}</code> · stage <code>{st_lbl}</code>\n"
            f"📷 still · 🎬 GIF · 🎥 video · 📎 other"
        )
        await message.reply_text(
            header,
            parse_mode="HTML",
            reply_markup=self._media_results_keyboard(rows),
        )

    @staticmethod
    def _media_kind_icon(kind: Optional[str]) -> str:
        k = (kind or "").lower().strip()
        if k in ("jpeg", "jpg", "png", "webp"):
            return "📷"
        if k == "gif":
            return "🎬"
        if k in ("mp4", "mkv", "avi", "mov", "webm"):
            return "🎥"
        return "📎"

    @staticmethod
    def _media_ts_short(created_raw: Any) -> str:
        if created_raw is None:
            return "??:??"
        s = str(created_raw).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None and dt.utcoffset() is not None:
                dt = dt.astimezone()
            return dt.strftime("%m-%d %H:%M")
        except ValueError:
            return str(created_raw)[:11]

    def _media_button_label(self, art: Dict[str, Any]) -> str:
        icon = self._media_kind_icon(art.get("kind"))
        cam = str(art.get("camera_id") or "?")[:12]
        ts = self._media_ts_short(art.get("created_at"))
        text = f"{icon} {cam} {ts}".strip()
        if len(text) > 64:
            text = text[:61] + "…"
        return text

    def _media_results_keyboard(
        self, artifacts: List[Dict[str, Any]]
    ) -> InlineKeyboardMarkup:
        """Up to 40 one-tap buttons; Telegram allows 100 inline buttons total."""
        grid: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        for art in artifacts:
            aid = art.get("id")
            if aid is None:
                continue
            try:
                aid_int = int(aid)
            except (TypeError, ValueError):
                continue
            payload = self._cb_data("mf", str(aid_int))
            if len(payload.encode("utf-8")) > 64:
                continue
            label = self._media_button_label(art)
            row.append(InlineKeyboardButton(label, callback_data=payload))
            if len(row) >= 3:
                grid.append(row)
                row = []
        if row:
            grid.append(row)
        return InlineKeyboardMarkup(grid)

    def _media_delivery_caption(
        self,
        meta: Optional[Dict[str, Any]],
        artifact_id: int,
    ) -> str:
        """Caption under delivered media: 💾 id · camera · stage · kind icon · time."""
        if not meta:
            return f"💾 <b>#{artifact_id}</b>"
        aid = meta.get("id", artifact_id)
        cam = meta.get("camera_id") or "?"
        stage = meta.get("stage") or "?"
        icon = self._media_kind_icon(meta.get("kind"))
        ts = self._media_ts_short(meta.get("created_at"))
        return f"💾 <b>#{aid}</b> <code>{cam}</code> <code>{stage}</code> {icon} <code>{ts}</code>"

    @staticmethod
    def _classify_file_for_telegram(content: bytes, content_type: str) -> str:
        ct = (content_type or "").lower()
        if (
            "image/jpeg" in ct
            or "image/jpg" in ct
            or "image/png" in ct
            or "image/webp" in ct
        ):
            return "photo"
        if "image/gif" in ct:
            return "animation"
        if len(content) >= 6 and content[:6] in (b"GIF87a", b"GIF89a"):
            return "animation"
        if "video/" in ct:
            return "video"
        if ct.startswith("image/"):
            return "photo"
        return "document"

    async def _send_media_artifact(
        self,
        message,
        artifact_id: int,
        user_id: Optional[int],
    ) -> None:
        """Fetch indexed media from API and send to chat."""
        meta: Optional[Dict[str, Any]] = None
        try:
            meta = await self._http_api.get_media_meta(artifact_id, user_id=user_id)
        except (httpx.HTTPStatusError, httpx.RequestError):
            meta = None
        try:
            content, content_type = await self._http_api.get_media_file_bytes(
                artifact_id, user_id=user_id
            )
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await message.reply_text(f"❌ Media #{artifact_id}: {detail}")
            return
        except httpx.RequestError as e:
            await message.reply_text(f"❌ API unreachable: {e!s}")
            return
        if not content:
            await message.reply_text(f"❌ Media #{artifact_id}: empty file.")
            return

        kind = self._classify_file_for_telegram(content, content_type)
        caption = self._media_delivery_caption(meta, artifact_id)
        bio = io.BytesIO(content)
        try:
            if kind == "photo":
                bio.name = "image.jpg"
                await message.reply_photo(photo=bio, caption=caption, parse_mode="HTML")
                return
            if kind == "animation":
                bio.name = "clip.gif"
                await message.reply_animation(
                    animation=bio, caption=caption, parse_mode="HTML"
                )
                return
            if kind == "video":
                bio.name = "clip.mp4"
                await message.reply_video(video=bio, caption=caption, parse_mode="HTML")
                return
            bio.name = f"artifact_{artifact_id}.bin"
            await message.reply_document(
                document=bio, caption=caption, parse_mode="HTML"
            )
        except TelegramError as ex:
            self.logger.warning("send media #%s failed: %s", artifact_id, ex)
            await message.reply_text(f"❌ Could not send media #{artifact_id} in chat.")

    @_require_authorization
    async def cmd_media(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List recent media artifacts via HTTP API, or open filter buttons when called with no args."""
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> (and optional API key) to use /media.",
                parse_mode="HTML",
            )
            return

        args = (context.args or []) if context else []
        user_id = update.effective_user.id if update.effective_user else None

        if not args:
            try:
                st = await self._http_api.get_status(user_id=user_id)
            except httpx.HTTPStatusError as e:
                detail = ""
                try:
                    detail = e.response.json().get("detail", str(e))
                except Exception:
                    detail = e.response.text or str(e)
                await update.message.reply_text(f"❌ API error: {detail}")
                return
            except httpx.RequestError as e:
                await update.message.reply_text(f"❌ API unreachable: {e!s}")
                return
            cams = st.get("camera_ids") or []
            if not cams:
                await update.message.reply_text(
                    "🗂 Choose filters — no cameras in status; use:\n<code>/media - -</code> for all, or <code>/media cam stage</code>",
                    parse_mode="HTML",
                )
                return
            await update.message.reply_text(
                "🗂 <b>Media</b>\nStep 1 — pick a camera:",
                parse_mode="HTML",
                reply_markup=self._media_camera_keyboard(cams),
            )
            return

        camera_id = args[0].strip() if len(args) >= 1 else None
        stage = args[1].strip() if len(args) >= 2 else None
        if camera_id == "-":
            camera_id = None
        if stage == "-":
            stage = None
        await self._send_media_list(
            update.message, context, camera_id=camera_id, stage=stage, user_id=user_id
        )

    async def _snap_send_photo(
        self,
        message,
        camera_id: str,
        user_id: Optional[int],
    ) -> None:
        try:
            out = await self._http_api.snap(camera_id, user_id=user_id)
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                body = e.response.json()
                detail = body.get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await message.reply_text(f"❌ Snap failed: {detail}")
            return
        except httpx.RequestError as e:
            await message.reply_text(f"❌ API unreachable: {e!s}")
            return
        path_str = out.get("path") or ""
        caption = f"📷 <b>Snap</b> · <code>{camera_id}</code>"
        if path_str:
            p = Path(path_str)
            if p.is_file():
                try:
                    with open(p, "rb") as f:
                        await message.reply_photo(
                            photo=f,
                            caption=caption,
                            parse_mode="HTML",
                        )
                    return
                except TelegramError as ex:
                    self.logger.warning(
                        "reply_photo failed for snap camera=%s path=%s: %s",
                        camera_id,
                        path_str,
                        ex,
                    )
        if path_str:
            self.logger.info("snap written but not sent in chat: %s", path_str)
        await message.reply_text(
            "📷 <b>Snap</b> saved but the image could not be sent here. "
            f"Camera <code>{camera_id}</code> — check logs or <code>/media</code>.",
            parse_mode="HTML",
        )

    @_require_authorization
    async def cmd_snap(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Request an on-demand JPEG snap via HTTP API; no args → pick camera with buttons."""
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> to use /snap.",
                parse_mode="HTML",
            )
            return
        args = (context.args or []) if context else []
        user_id = update.effective_user.id if update.effective_user else None
        if not args:
            try:
                st = await self._http_api.get_status(user_id=user_id)
            except httpx.HTTPStatusError as e:
                detail = ""
                try:
                    detail = e.response.json().get("detail", str(e))
                except Exception:
                    detail = e.response.text or str(e)
                await update.message.reply_text(f"❌ API error: {detail}")
                return
            except httpx.RequestError as e:
                await update.message.reply_text(f"❌ API unreachable: {e!s}")
                return
            cams = st.get("camera_ids") or []
            if not cams:
                await update.message.reply_text(
                    "Usage: <code>/snap &lt;camera_id&gt;</code>",
                    parse_mode="HTML",
                )
                return
            await update.message.reply_text(
                "📷 <b>Snap</b> — choose camera:",
                parse_mode="HTML",
                reply_markup=self._snap_camera_keyboard(cams),
            )
            return
        camera_id = args[0].strip()
        await self._snap_send_photo(update.message, camera_id, user_id)

    @staticmethod
    def _format_config_value_telegram(v: Any) -> str:
        if v is None:
            return "—"
        r = repr(v)
        return (r[:117] + "…") if len(r) > 120 else r

    @_require_authorization
    async def cmd_show_config(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List SQLite tunables with types, stored values, live patrol, recipe notify defaults."""
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> (and optional API key) to use /show_config.",
                parse_mode="HTML",
            )
            return
        user_id = update.effective_user.id if update.effective_user else None
        try:
            cfg, traits, st = await asyncio.gather(
                self._http_api.get_all_config(user_id=user_id),
                self._http_api.get_config_traits(user_id=user_id),
                self._http_api.get_status(user_id=user_id),
            )
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await update.message.reply_text(f"❌ API error: {detail}")
            return
        except httpx.RequestError as e:
            await update.message.reply_text(f"❌ API unreachable: {e!s}")
            return

        cfg = cfg if isinstance(cfg, dict) else {}
        traits = traits if isinstance(traits, dict) else {}
        st = st if isinstance(st, dict) else {}
        catalog_keys = {entry[0] for entry in _SQLITE_CONFIG_KNOBS}
        live_patrol = st.get("patrol_time")

        lines: List[str] = [
            "⚙️ <b>Config - tunables</b>\n",
            "Use <code>/config key value</code> (YAML).\n",
            "Example: <code>/config patrol_time 10</code>\n\n",
        ]
        for key, typ, desc in _SQLITE_CONFIG_KNOBS:
            stored = cfg.get(key)
            disp = self._format_config_value_telegram(stored) if key in cfg else "—"
            tr = traits.get(key) if isinstance(traits.get(key), dict) else {}
            mode = (
                "hot-swappable"
                if bool(tr.get("hot_swappable", True))
                else "restart-required"
            )
            lines.append(f"<b>{key}</b>")
            lines.append(f"\n• type: <code>{typ}</code>")
            lines.append(f"\n• mode: <i>{mode}</i>")
            lines.append(f"\n• stored: <code>{disp}</code>")
            lines.append(f"\n• notes: {desc}")
            if key == "patrol_time" and live_patrol is not None:
                lines.append(f"\n• live patrol: <code>{live_patrol}</code>s")
            if key.startswith("notify_on_"):
                lines.append(
                    f"\n• recipe default: <code>{self._recipe_notify_preview(key)}</code>"
                )
            lines.append("\n\n")

        extras = sorted(k for k in cfg if k not in catalog_keys)
        if extras:
            lines.append("<b>Other DB keys</b>\n")
            for ek in extras:
                lines.append(
                    f"• <code>{ek}</code> = <code>{self._format_config_value_telegram(cfg[ek])}</code>\n"
                )
            lines.append("")

        if st:
            lines.append("<b>Runtime</b> (read-only)\n")
            lines.append(
                f"paused <code>{st.get('paused')}</code> · cycles <code>{st.get('total_cycles')}</code>\n"
            )
            rs = st.get("restart_schedule") or {}
            if isinstance(rs, dict) and rs.get("scheduled"):
                lines.append(
                    "restart scheduled at "
                    f"<code>{rs.get('scheduled_at')}</code> "
                    f"(in ~<code>{rs.get('seconds_until_restart')}</code>s)\n"
                )
            cids = st.get("camera_ids") or []
            if cids:
                lines.append(
                    "cameras " + " ".join(f"<code>{c}</code>" for c in cids) + "\n"
                )

        body = "".join(lines)
        if len(body) > 4000:
            body = body[:3990] + "\n…(truncated)"
        await update.message.reply_text(body, parse_mode="HTML")

    @_require_authorization
    async def cmd_config(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Update a single config key via /api/config/{key}."""
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> (and optional API key) to use /config.",
                parse_mode="HTML",
            )
            return
        args = (context.args or []) if context else []
        if len(args) == 1:
            key_only = args[0].strip()
            await update.message.reply_text(
                "⚠️ Missing value for "
                f"<code>{key_only}</code>.\n"
                "Use <code>/config key value</code> (YAML), e.g.:\n"
                '<code>/config notify_on_detection ["text"]</code>\n'
                "<code>/config notify_on_detection []</code> (disable)\n"
                "<code>/config notify_on_detection none</code> (disable)",
                parse_mode="HTML",
            )
            return
        if len(args) < 2:
            await self.cmd_show_config(update, context)
            return
        key = args[0].strip()
        raw_value = " ".join(args[1:]).strip()
        try:
            parsed_value = yaml.safe_load(raw_value)
        except Exception:
            parsed_value = raw_value
        user_id = update.effective_user.id if update.effective_user else None
        try:
            out = await self._http_api.set_config_value(
                key, parsed_value, user_id=user_id
            )
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await update.message.reply_text(f"❌ Config update failed: {detail}")
            return
        except httpx.RequestError as e:
            await update.message.reply_text(f"❌ API unreachable: {e!s}")
            return
        value_repr = repr(out.get("value"))
        msg = (
            f"✅ Updated <code>{out.get('key', key)}</code> = <code>{value_repr}</code>"
        )
        rs = out.get("restart_schedule") if isinstance(out, dict) else None
        if isinstance(rs, dict) and rs.get("scheduled"):
            if rs.get("newly_scheduled"):
                msg += (
                    "\n🔁 Restart scheduled at "
                    f"<code>{rs.get('scheduled_at')}</code> "
                    f"(~<code>{rs.get('seconds_until_restart')}</code>s)."
                )
            else:
                msg += (
                    "\nℹ️ Restart already scheduled at "
                    f"<code>{rs.get('scheduled_at')}</code> "
                    f"(~<code>{rs.get('seconds_until_restart')}</code>s)."
                )
        await update.message.reply_text(msg, parse_mode="HTML")

    @_require_authorization
    async def cmd_config_reset(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset one config override key, or all keys, via /api/config/reset."""
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> (and optional API key) to use /config_reset.",
                parse_mode="HTML",
            )
            return
        args = (context.args or []) if context else []
        if not args:
            await update.message.reply_text(
                "Usage:\n"
                "<code>/config_reset &lt;key&gt;</code>\n"
                "<code>/config_reset all</code>",
                parse_mode="HTML",
            )
            return
        raw = " ".join(args).strip()
        reset_all = raw.lower() in {"all", "*"}
        key = None if reset_all else raw
        user_id = update.effective_user.id if update.effective_user else None
        try:
            out = await self._http_api.reset_config(
                key=key,
                reset_all=reset_all,
                user_id=user_id,
            )
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await update.message.reply_text(f"❌ Config reset failed: {detail}")
            return
        except httpx.RequestError as e:
            await update.message.reply_text(f"❌ API unreachable: {e!s}")
            return
        scope = out.get("scope", "single")
        removed = int(out.get("removed", 0) or 0)
        target = (
            "all keys"
            if scope == "all"
            else f"key <code>{html.escape(str(key or ''))}</code>"
        )
        msg = f"🧹 Reset {target}: removed <code>{removed}</code> override row(s)."
        rs = out.get("restart_schedule") if isinstance(out, dict) else None
        if isinstance(rs, dict) and rs.get("scheduled"):
            if rs.get("newly_scheduled"):
                msg += (
                    "\n🔁 Restart scheduled at "
                    f"<code>{rs.get('scheduled_at')}</code> "
                    f"(~<code>{rs.get('seconds_until_restart')}</code>s)."
                )
            else:
                msg += (
                    "\nℹ️ Restart already scheduled at "
                    f"<code>{rs.get('scheduled_at')}</code> "
                    f"(~<code>{rs.get('seconds_until_restart')}</code>s)."
                )
        await update.message.reply_text(msg, parse_mode="HTML")

    @_require_authorization
    async def cmd_test(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /test command."""
        test_event = NotificationEvent(
            message="🧪 Test notification",
            event_type="test",
            stage="detection",
        )
        self._queue_notification(test_event)
        await update.message.reply_text("📨 Test notification queued!")

    @staticmethod
    def _uuid_from_compact(s: str) -> Optional[str]:
        raw = (s or "").strip()
        if len(raw) == 36 and re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
            raw,
        ):
            return raw
        if len(raw) == 32 and re.match(r"^[0-9a-fA-F]+$", raw, re.I):
            h = raw.lower()
            return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
        return None

    @_require_authorization
    async def cmd_face_assign(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Assign a pending unknown face to a new or existing identity (HTTP API)."""
        if not update.message:
            return
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> to use /face_assign.",
                parse_mode="HTML",
            )
            return
        args = (context.args or []) if context else []
        user_id = update.effective_user.id if update.effective_user else None
        if len(args) < 2:
            await update.message.reply_text(
                "Usage:\n"
                "<code>/face_assign &lt;ref_or_pending_id&gt; &lt;display_name&gt;</code>\n"
                "or <code>/face_assign … --id &lt;identity_id&gt;</code>\n"
                "Short <code>REF</code> from unknown-face alerts: <code>/fa REF Name</code>",
                parse_mode="HTML",
            )
            return
        pending_id = self._expand_pending_token(args[0].strip()) or args[0].strip()
        identity_id: Optional[str] = None
        new_display_name: Optional[str] = None
        if len(args) >= 3 and args[1].strip().lower() in ("--id", "--identity"):
            identity_id = args[2].strip()
        else:
            new_display_name = " ".join(args[1:]).strip()
        try:
            await self._http_api.assign_pending_face(
                pending_id,
                identity_id=identity_id,
                new_display_name=new_display_name,
                user_id=user_id,
            )
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await update.message.reply_text(f"❌ Assign failed: {detail}")
            return
        except httpx.RequestError as e:
            await update.message.reply_text(f"❌ API unreachable: {e!s}")
            return
        await update.message.reply_text("✅ Pending face assigned to the gallery.")

    @_require_authorization
    async def cmd_face_ignore(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Ignore a pending unknown face (HTTP API)."""
        if not update.message:
            return
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> to use /face_ignore.",
                parse_mode="HTML",
            )
            return
        args = (context.args or []) if context else []
        user_id = update.effective_user.id if update.effective_user else None
        if len(args) != 1:
            await update.message.reply_text(
                "Usage: <code>/face_ignore &lt;ref_or_pending_id&gt;</code>",
                parse_mode="HTML",
            )
            return
        pending_id = self._expand_pending_token(args[0].strip()) or args[0].strip()
        try:
            await self._http_api.ignore_pending_face(pending_id, user_id=user_id)
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await update.message.reply_text(f"❌ Ignore failed: {detail}")
            return
        except httpx.RequestError as e:
            await update.message.reply_text(f"❌ API unreachable: {e!s}")
            return
        await update.message.reply_text("✅ Pending face ignored.")

    async def _send_stats_hours(
        self,
        message,
        *,
        hours: int,
        user_id: Optional[int],
    ) -> None:
        """Analytics summary + trend chart (same HTTP routes as dashboard charts)."""
        try:
            data = await self._http_api.get_analytics_summary(
                hours=hours, user_id=user_id
            )
            chart_bytes = await self._http_api.get_analytics_chart_jpeg(
                hours=hours, user_id=user_id
            )
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await message.reply_text(
                f"❌ API error ({e.response.status_code}): {detail}"
            )
            return
        except httpx.RequestError as e:
            await message.reply_text(f"❌ Cannot reach Spyoncino API.\n{e!s}")
            return

        metrics = data.get("metrics") or {}
        window = data.get("window") or {}
        by_type = window.get("by_type") or {}
        txt = (
            f"📈 <b>Stats</b> ({hours}h) <i>(API)</i>\n"
            f"Events in window: <b>{window.get('events_total', 0)}</b>\n"
            f"Motion: {by_type.get('motion', 0)} | Person: {by_type.get('person', 0)} | "
            f"Face: {by_type.get('face', 0)} | Warnings: {window.get('warnings', 0)} | "
            f"Errors: {window.get('errors', 0)}\n"
            f"Lifetime totals — M:{metrics.get('motion_events', 0)} "
            f"P:{metrics.get('person_events', 0)} F:{metrics.get('face_events', 0)}"
        )
        if chart_bytes:
            await message.reply_photo(
                photo=io.BytesIO(chart_bytes),
                caption=txt,
                parse_mode="HTML",
            )
        else:
            await message.reply_text(txt, parse_mode="HTML")

    @_require_authorization
    async def cmd_stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Analytics window + trend chart via HTTP API (replaces former /analytics)."""
        if not self._http_api:
            await update.message.reply_text(
                "❌ Set recipe <code>api_base_url</code> (and optional API key) to use /stats.",
                parse_mode="HTML",
            )
            return
        args = (context.args or []) if context else []
        hours = 24
        if args:
            try:
                hours = max(1, min(168, int(args[0])))
            except ValueError:
                await update.message.reply_text(
                    "Usage: <code>/stats [hours]</code>\nExample: <code>/stats 24</code>",
                    parse_mode="HTML",
                )
                return
        user_id = update.effective_user.id if update.effective_user else None
        await self._send_stats_hours(update.message, hours=hours, user_id=user_id)

    async def _on_menu_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Inline keyboard actions (prefix sc|)."""
        q = update.callback_query
        if not q or not q.message:
            return
        user = q.from_user
        if not user:
            await q.answer()
            return
        await q.answer()
        if self._is_group_context(update) and not self._allow_group_commands:
            return
        if self._is_group_context(update) and not self._is_allowed_group_command_chat(
            update
        ):
            return
        if not self._can_telegram(user.id, "view_status"):
            await q.answer("Unauthorized", show_alert=True)
            return
        if not self._http_api:
            await q.message.reply_text("❌ API not configured.")
            return

        parts = (q.data or "").split("|")
        if len(parts) < 2 or parts[0] != self._cb:
            return
        action = parts[1]
        user_id = user.id

        try:
            if action == "st":
                data = await self._http_api.get_status(user_id=user_id)
                text = self._format_status_html(data)
                await q.message.reply_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=self._main_menu_keyboard(),
                )
                return
            if action == "hp":
                await q.message.reply_text(
                    "<b>Help</b>\nUse /help for the full command list.",
                    parse_mode="HTML",
                    reply_markup=self._main_menu_keyboard(),
                )
                return
            if action == "md":
                st = await self._http_api.get_status(user_id=user_id)
                cams = st.get("camera_ids") or []
                if not cams:
                    await q.message.reply_text(
                        "🗂 No cameras in status. Try <code>/media - -</code>.",
                        parse_mode="HTML",
                    )
                    return
                await q.message.reply_text(
                    "🗂 <b>Media</b> — pick a camera:",
                    parse_mode="HTML",
                    reply_markup=self._media_camera_keyboard(cams),
                )
                return
            if action == "mc" and len(parts) >= 3:
                raw = parts[2]
                cam = None if raw == _MEDIA_LIST_ALL else raw
                context.user_data["media_filter_camera"] = cam
                await q.message.reply_text(
                    "🗂 <b>Media</b> — pick a stage:",
                    parse_mode="HTML",
                    reply_markup=self._media_stage_keyboard(),
                )
                return
            if action == "ms" and len(parts) >= 3:
                stage_token = parts[2]
                stage = None if stage_token == _MEDIA_LIST_ALL else stage_token
                cam = context.user_data.get("media_filter_camera")
                await self._send_media_list(
                    q.message, context, camera_id=cam, stage=stage, user_id=user_id
                )
                return
            if action == "mf" and len(parts) >= 3:
                try:
                    aid = int(parts[2])
                except ValueError:
                    await q.message.reply_text("Invalid media id.")
                    return
                await self._send_media_artifact(q.message, aid, user_id)
                return
            if action == "sn":
                if len(parts) >= 3 and parts[2]:
                    await self._snap_send_photo(q.message, parts[2], user_id)
                    return
                st = await self._http_api.get_status(user_id=user_id)
                cams = st.get("camera_ids") or []
                if not cams:
                    await q.message.reply_text("No cameras in status.")
                    return
                await q.message.reply_text(
                    "📷 <b>Snap</b> — choose camera:",
                    parse_mode="HTML",
                    reply_markup=self._snap_camera_keyboard(cams),
                )
                return
            if action == "ps":
                st = await self._http_api.get_status(user_id=user_id)
                new_paused = not bool(st.get("paused"))
                out = await self._http_api.set_paused(new_paused, user_id=user_id)
                self._pause_reminder_was_paused = False
                self._pause_reminder_accumulator_s = 0.0
                await q.message.reply_text(
                    f"⏸ Patrol is now <b>{'paused' if out.get('paused') else 'running'}</b>.",
                    parse_mode="HTML",
                    reply_markup=self._main_menu_keyboard(),
                )
                return
            if action == "ss":
                await self._send_stats_hours(q.message, hours=24, user_id=user_id)
                return
            if action in ("ig", "fi") and len(parts) >= 3:
                raw = parts[2]
                pid = self._expand_pending_token(raw) or self._uuid_from_compact(raw)
                if not pid:
                    await q.message.reply_text("Invalid ref or pending id.")
                    return
                try:
                    await self._http_api.ignore_pending_face(pid, user_id=user_id)
                except httpx.HTTPStatusError as e:
                    detail = ""
                    try:
                        detail = e.response.json().get("detail", str(e))
                    except Exception:
                        detail = e.response.text or str(e)
                    await q.message.reply_text(f"❌ Ignore failed: {detail}")
                    return
                except httpx.RequestError as e:
                    await q.message.reply_text(f"❌ API unreachable: {e!s}")
                    return
                await q.message.reply_text("✅ Pending face ignored.")
                return
            if action == "as" and len(parts) >= 4:
                ref = parts[2].strip().lower()
                idpfx = parts[3].strip().lower()
                pid = self._expand_pending_token(ref) or self._uuid_from_compact(ref)
                if not pid:
                    await q.message.reply_text(
                        "Unknown ref — try the full id from the dashboard."
                    )
                    return
                iid = self._resolve_identity_hex_prefix(idpfx)
                if not iid:
                    await q.message.reply_text(
                        "Could not match that identity. Use <code>/face_assign … --id …</code>.",
                        parse_mode="HTML",
                    )
                    return
                try:
                    await self._http_api.assign_pending_face(
                        pid,
                        identity_id=iid,
                        new_display_name=None,
                        user_id=user_id,
                    )
                except httpx.HTTPStatusError as e:
                    detail = ""
                    try:
                        detail = e.response.json().get("detail", str(e))
                    except Exception:
                        detail = e.response.text or str(e)
                    await q.message.reply_text(f"❌ Assign failed: {detail}")
                    return
                except httpx.RequestError as e:
                    await q.message.reply_text(f"❌ API unreachable: {e!s}")
                    return
                await q.message.reply_text("✅ Assigned to existing identity.")
                return
            if action == "nw" and len(parts) >= 3:
                ref = parts[2].strip().lower()
                if len(ref) == 8 and re.match(r"^[0-9a-f]{8}$", ref):
                    disp = ref.upper()
                    await q.message.reply_text(
                        f"New person: <code>/fa {disp} Their name</code>\n"
                        f"(same as <code>/face_assign {disp} Their name</code>)",
                        parse_mode="HTML",
                    )
                else:
                    await q.message.reply_text("Invalid ref.")
                return
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            await q.message.reply_text(f"❌ API error: {detail}")
        except httpx.RequestError as e:
            await q.message.reply_text(f"❌ API unreachable: {e!s}")

    async def _pause_reminder_tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Notify notification chat while patrol stays paused (interval ≈ max(120s, 100×patrol_time))."""
        if not self._http_api:
            return
        target = self._notification_target_chat_id() or self.chat_id
        if not target:
            return
        try:
            st = await self._http_api.get_status(user_id=None)
        except Exception:
            return
        paused = bool(st.get("paused"))
        patrol = float(st.get("patrol_time") or 5.0)
        interval = max(120.0, 100.0 * patrol)

        if not paused:
            self._pause_reminder_was_paused = False
            self._pause_reminder_accumulator_s = 0.0
            return

        if not self._pause_reminder_was_paused:
            self._pause_reminder_was_paused = True
            self._pause_reminder_accumulator_s = 0.0

        self._pause_reminder_accumulator_s += 60.0
        if self._pause_reminder_accumulator_s < interval:
            return
        self._pause_reminder_accumulator_s = 0.0
        try:
            await context.bot.send_message(
                chat_id=target,
                text=(
                    "⏸ <b>Reminder:</b> patrol is still <b>paused</b>.\n"
                    f"Next reminder in ~{int(interval)}s (100× patrol_time).\n"
                    "Resume with <code>/pause</code> or the menu button."
                ),
                parse_mode="HTML",
            )
        except TelegramError as e:
            self.logger.warning("pause reminder send failed: %s", e)

    def _effective_notification_rate_limit(self) -> int:
        base = max(1, int(self.notification_rate_limit))
        if not self.memory_manager:
            return base
        raw = self.memory_manager.get_config("notification_rate_limit", None)
        if raw is None:
            return base
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return base

    def _merge_telegram_flood_until(self, seconds: float) -> None:
        """Honor Telegram Bot API flood control (429 + Retry-After), independent of local counters."""
        until = time.monotonic() + max(0.0, float(seconds)) + 0.5
        if self._telegram_flood_until is None or until > self._telegram_flood_until:
            self._telegram_flood_until = until
        self.logger.warning(
            "Telegram flood control: outbound paused ~%.0fs (API RetryAfter)", seconds
        )

    def _telegram_flood_seconds_remaining(self) -> float:
        if self._telegram_flood_until is None:
            return 0.0
        rem = self._telegram_flood_until - time.monotonic()
        if rem <= 0:
            self._telegram_flood_until = None
            return 0.0
        return rem

    def _is_rate_limited(self) -> bool:
        """True if local rolling cap is full or Telegram returned flood control (429)."""
        if self._telegram_flood_until is not None:
            if time.monotonic() < self._telegram_flood_until:
                return True
            self._telegram_flood_until = None
        now = datetime.now()
        # Remove notifications older than 1 minute
        self._notification_times = [
            t for t in self._notification_times if (now - t).total_seconds() < 60
        ]
        return (
            len(self._notification_times) >= self._effective_notification_rate_limit()
        )

    def _recipe_notify_preview(self, db_key: str) -> str:
        m = {
            "notify_on_preproc": self.notify_preproc,
            "notify_on_detection": self.notify_detection,
            "notify_on_face": self.notify_face,
        }
        s = m.get(db_key, set())
        return ",".join(sorted(s)) if s else "none"

    def _modes_for_stage(self, stage: str) -> Set[str]:
        key_map = {
            "preproc": ("notify_on_preproc", self.notify_preproc),
            "detection": ("notify_on_detection", self.notify_detection),
            "face": ("notify_on_face", self.notify_face),
        }
        entry = key_map.get(stage)
        if not entry:
            return set()
        db_key, fallback = entry
        if not self.memory_manager:
            return set(fallback)
        raw = self.memory_manager.get_config(db_key, None)
        if raw is None:
            return set(fallback)
        try:
            return normalize_notify_modes(raw)
        except ValueError as e:
            self.logger.warning("Invalid %s in SQLite config: %s", db_key, e)
            return set(fallback)

    def _max_frames_for_modes(self, modes: Set[str]) -> int:
        n = 1
        if "gif" in modes:
            n = max(n, int(self.gif_duration * self.gif_fps))
        if "video" in modes:
            n = max(n, int(self.video_duration * self.video_fps))
        return max(n, 1)

    def _is_mergeable_text_notification(self, event: NotificationEvent) -> bool:
        """True if this alert would be sent as text only (safe to merge with others)."""
        if event.photo_path:
            return False
        if event.event_type == "test":
            return True
        if event.prefer_plain_text:
            return True
        modes = self._modes_for_stage(event.stage)
        if not modes:
            return True
        has_frames = bool(event.frames and len(event.frames) > 0)
        if "video" in modes and has_frames:
            return False
        if "gif" in modes and has_frames:
            return False
        return True

    async def _send_merged_text_notifications(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        events: List[NotificationEvent],
        target_chat_id: int,
    ) -> None:
        """Send several text-only alerts in one Telegram message."""
        parts: List[str] = []
        for e in events:
            ts = e.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            if e.prefer_plain_text:
                parts.append(f"{e.message}\n📅 {ts}")
            else:
                parts.append(f"<b>{e.message}</b>\n📅 {ts}")
        header = f"<b>{len(events)} alerts</b>\n\n"
        body = header + "\n\n".join(parts)
        if len(body) > 3900:
            body = body[:3880] + "\n\n<i>…truncated</i>"
        try:
            await context.bot.send_message(
                chat_id=target_chat_id,
                text=body,
                parse_mode="HTML",
                read_timeout=30,
                write_timeout=60,
            )
        except RetryAfter as e:
            self._merge_telegram_flood_until(float(e.retry_after))
            raise

    async def _flush_notification_batch(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        batch: List[NotificationEvent],
        target_chat_id: int,
    ) -> None:
        """Deliver a batch: consecutive text-only rows merged; GIF/video/photo stay separate."""
        i = 0
        n = len(batch)
        while i < n:
            if self._is_rate_limited():
                self._requeue_front.extend(batch[i:])
                break
            if self._is_mergeable_text_notification(batch[i]):
                run: List[NotificationEvent] = []
                while i < n and self._is_mergeable_text_notification(batch[i]):
                    run.append(batch[i])
                    i += 1
                if not run:
                    continue
                try:
                    await self._send_merged_text_notifications(
                        context, run, target_chat_id
                    )
                    self.notification_stats["sent"] += 1
                    self._notification_times.append(datetime.now())
                except RetryAfter:
                    for ev_rem in reversed(run):
                        self._requeue_front.appendleft(ev_rem)
                    for ev_rem in reversed(batch[i:]):
                        self._requeue_front.appendleft(ev_rem)
                    return
                except Exception as e:
                    self.notification_stats["failed"] += 1
                    self.logger.error(
                        "Failed to send merged text notification: %s",
                        e,
                        exc_info=True,
                    )
                continue

            ev = batch[i]
            i += 1
            try:
                await self._send_notification(
                    context, ev, target_chat_id=target_chat_id
                )
                self.notification_stats["sent"] += 1
                self._notification_times.append(datetime.now())
            except RetryAfter:
                for ev_rem in reversed(batch[i - 1 :]):
                    self._requeue_front.appendleft(ev_rem)
                return
            except Exception as e:
                self.notification_stats["failed"] += 1
                self.logger.error("Failed to send notification: %s", e, exc_info=True)

    def _queue_notification(self, event: NotificationEvent) -> None:
        """Queue a notification event (never dropped for rate limiting — see drain)."""
        event.enqueued_at = datetime.now()
        try:
            self.notification_queue.put_nowait(event)
        except queue.Full:
            self.notification_stats["queue_dropped"] += 1
            self.logger.error(
                "Notification queue full (max %s); dropping alert",
                _NOTIFICATION_QUEUE_MAX,
            )
            return
        self.logger.debug("Queued notification: %s", event.message)

    def outbound_metrics(self) -> Dict[str, Any]:
        """Lightweight snapshot for /api/status (queue + send counters). Thread-safe reads."""
        try:
            qd = self.notification_queue.qsize()
        except Exception:
            qd = -1
        try:
            rq = len(self._requeue_front)
        except Exception:
            rq = -1
        er = int(self.notification_stats.get("emergency_recaps", 0))
        bs = int(self.notification_stats.get("batch_summaries", 0))
        return {
            "queue_depth": qd,
            "requeue_depth": rq,
            "outbound_strategy": self._outbound_strategy,
            "notifications_sent": int(self.notification_stats.get("sent", 0)),
            "notifications_failed": int(self.notification_stats.get("failed", 0)),
            "notifications_queue_dropped": int(
                self.notification_stats.get("queue_dropped", 0)
            ),
            "notifications_emergency_recaps": er,
            "notifications_batch_summaries": bs,
            "notifications_backlog_alerts_cleared": int(
                self.notification_stats.get("backlog_alerts_cleared", 0)
            ),
            # Deprecated aliases (older UI / scripts)
            "notifications_rate_limited": er,
            "notifications_clog_clears": er,
            "rate_limit_per_minute": self._effective_notification_rate_limit(),
            "telegram_flood_seconds_remaining": round(
                self._telegram_flood_seconds_remaining(), 1
            ),
        }

    async def _drain_notification_queue(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send pending notifications (caller must hold ``_notification_drain_lock``)."""
        try:
            target_chat_id = self._notification_target_chat_id()
            if not target_chat_id:
                self._discard_pending_notifications()
                self.logger.warning("No chat ID configured - notifications dropped")
                return

            if self.notification_queue.empty() and not self._requeue_front:
                return

            if self._is_rate_limited():
                pending = self._pending_notification_count()
                # Emergency: rate-limited and backlog huge → one recap message + clear (no stale GIF burst).
                if pending >= _EMERGENCY_RECAP_THRESHOLD:
                    discarded = self._drain_backlog_to_events()
                    cleared = len(discarded)
                    digest = self._format_clog_digest_html(discarded)
                    intro = (
                        "🚨 <b>Emergency recap</b> — Telegram rate limit active "
                        f"(≤{self._effective_notification_rate_limit()} msgs / 60s rolling) "
                        f"and backlog ≥ <b>{_EMERGENCY_RECAP_THRESHOLD}</b>.\n\n"
                        f"<b>{cleared}</b> alert(s) were <b>not</b> sent as GIF/video (would be stale). "
                        "Digest (camera · event · intended format):\n\n"
                    )
                    outro = "\n\n<i>Queue cleared. New alerts after this behave normally.</i>"
                    cap = intro + digest + outro
                    if len(cap) > 4090:
                        cap = (
                            cap[:4070]
                            + "\n\n<i>…truncated (Telegram length limit).</i>"
                        )
                    try:
                        await context.bot.send_message(
                            chat_id=target_chat_id,
                            text=cap,
                            parse_mode="HTML",
                            read_timeout=30,
                            write_timeout=60,
                        )
                    except RetryAfter as e:
                        self._merge_telegram_flood_until(float(e.retry_after))
                        for ev in reversed(discarded):
                            self._requeue_front.appendleft(ev)
                        self.logger.warning(
                            "Emergency recap not sent (Telegram flood); restored %s queued alert(s)",
                            cleared,
                        )
                        return
                    except Exception as e:
                        self.notification_stats["failed"] += 1
                        self.logger.error(
                            "Failed to send emergency recap: %s", e, exc_info=True
                        )
                        for ev in reversed(discarded):
                            self._requeue_front.appendleft(ev)
                        return
                    self.notification_stats["emergency_recaps"] += 1
                    self.notification_stats["backlog_alerts_cleared"] += cleared
                    self.notification_stats["sent"] += 1
                    self._notification_times.append(datetime.now())
                    self.logger.warning(
                        "Emergency notification recap (%s alerts cleared)", cleared
                    )
                    return
                # Otherwise wait for a send slot (no extra Telegram noise).
                return

            # Batch strategy: digest chunk(s) while backlog is high (skip per-alert GIF until smaller).
            if self._outbound_strategy == "batch":
                while (
                    self._pending_notification_count() >= _BATCH_DIGEST_THRESHOLD
                    and not self._is_rate_limited()
                ):
                    chunk = self._pop_events_from_backlog(_BATCH_DIGEST_CHUNK)
                    if not chunk:
                        break
                    try:
                        digest = self._format_clog_digest_html(chunk)
                        intro = (
                            "<b>Batch summary</b> — backlog ≥ "
                            f"{_BATCH_DIGEST_THRESHOLD}; this chunk is a <b>text recap</b> only "
                            f"(≤{len(chunk)} alert(s), no GIF/video for these):\n\n"
                        )
                        outro = "\n\n<i>Remaining queue will flush as GIF/video when depth drops.</i>"
                        cap = intro + digest + outro
                        if len(cap) > 4090:
                            cap = (
                                cap[:4070]
                                + "\n\n<i>…truncated (Telegram length limit).</i>"
                            )
                        await context.bot.send_message(
                            chat_id=target_chat_id,
                            text=cap,
                            parse_mode="HTML",
                            read_timeout=30,
                            write_timeout=60,
                        )
                        self.notification_stats["sent"] += 1
                        self.notification_stats["batch_summaries"] += 1
                        self._notification_times.append(datetime.now())
                    except RetryAfter as e:
                        self._merge_telegram_flood_until(float(e.retry_after))
                        for ev in reversed(chunk):
                            self._requeue_front.appendleft(ev)
                        break
                    except Exception as e:
                        self.notification_stats["failed"] += 1
                        self.logger.error(
                            "Failed to send batch digest: %s", e, exc_info=True
                        )
                        for ev in reversed(chunk):
                            self._requeue_front.appendleft(ev)
                        break
                    if self._is_rate_limited():
                        break

            batch: List[NotificationEvent] = []
            while len(batch) < _NOTIFICATION_DRAIN_BATCH_MAX:
                if self._requeue_front:
                    batch.append(self._requeue_front.popleft())
                    continue
                try:
                    batch.append(self.notification_queue.get_nowait())
                except queue.Empty:
                    break

            if not batch:
                return

            await self._flush_notification_batch(context, batch, target_chat_id)

        except Exception as e:
            self.logger.error(
                "Error processing notification queue: %s", e, exc_info=True
            )

    def _pending_notification_count(self) -> int:
        """Alerts waiting to send (requeue + main queue)."""
        try:
            return len(self._requeue_front) + self.notification_queue.qsize()
        except Exception:
            return len(self._requeue_front)

    def _drain_backlog_to_events(self) -> List[NotificationEvent]:
        """Remove every pending alert (requeue + queue) and return them in order."""
        out: List[NotificationEvent] = []
        while self._requeue_front:
            out.append(self._requeue_front.popleft())
        while True:
            try:
                out.append(self.notification_queue.get_nowait())
            except queue.Empty:
                break
        return out

    def _pop_events_from_backlog(self, max_n: int) -> List[NotificationEvent]:
        """Take up to ``max_n`` events (requeue first, then queue), FIFO."""
        out: List[NotificationEvent] = []
        while len(out) < max_n and self._requeue_front:
            out.append(self._requeue_front.popleft())
        while len(out) < max_n:
            try:
                out.append(self.notification_queue.get_nowait())
            except queue.Empty:
                break
        return out

    def _digest_plain_preview(self, message: str, max_len: int = 96) -> str:
        plain = re.sub(r"<[^>]+>", "", message or "")
        plain = " ".join(plain.split())
        if len(plain) > max_len:
            plain = plain[: max_len - 1] + "…"
        return html.escape(plain) if plain else "(no text)"

    def _digest_event_kind_label(self, event: NotificationEvent) -> str:
        """Human-readable alert category (motion vs person vs face, …)."""
        if event.event_type == "test":
            return "Test / ping"
        st = (event.stage or "").strip()
        if st == "preproc":
            return "Motion"
        if st == "detection":
            return "Person detection"
        if st == "face":
            return "Face"
        et = (event.event_type or "").strip()
        if et == "motion":
            return "Motion"
        if et in ("person",):
            return "Person detection"
        if et == "face":
            return "Face"
        return st or et or "Alert"

    def _digest_delivery_label(self, event: NotificationEvent) -> str:
        """What would have been sent on Telegram (media vs text)."""
        if event.photo_path:
            return "Photo with caption"
        if event.prefer_plain_text:
            return "Text"
        modes = (
            {"text"}
            if event.event_type == "test"
            else self._modes_for_stage(event.stage)
        )
        has_frames = bool(event.frames and len(event.frames) > 0)
        if "video" in modes and has_frames:
            return "Video clip"
        if "gif" in modes and has_frames:
            return "GIF"
        return "Text"

    def _digest_line_for_event(self, event: NotificationEvent) -> str:
        ts = event.timestamp.strftime("%Y-%m-%d %H:%M:%S") if event.timestamp else "?"
        cam = html.escape(str(event.camera_id or "?"))
        kind = html.escape(self._digest_event_kind_label(event))
        delivery = html.escape(self._digest_delivery_label(event))
        prev = self._digest_plain_preview(event.message)
        return (
            f"• <b>Camera</b> <code>{cam}</code>\n"
            f"  <b>Event</b> {kind} · <b>Would send</b> {delivery}\n"
            f"  <b>Time</b> {ts}\n"
            f"  <i>{prev}</i>"
        )

    def _format_clog_digest_html(self, events: List[NotificationEvent]) -> str:
        """Compact HTML list of queued alerts: camera, event type, intended format (no media)."""
        if not events:
            return "<i>(empty)</i>"
        max_lines = 15
        lines: List[str] = []
        for ev in events[:max_lines]:
            lines.append(self._digest_line_for_event(ev))
        body = "\n".join(lines)
        rest = len(events) - max_lines
        if rest > 0:
            body += f"\n\n<i>…and {rest} more not shown.</i>"
        return body

    def _clear_notification_backlog(self) -> int:
        """
        Drop all queued alerts; return how many were removed.
        Used when backlog is clogged so stale notifications do not arrive in a burst later.
        """
        return len(self._drain_backlog_to_events())

    def _discard_pending_notifications(self) -> None:
        """Clear queue + front requeue when there is no chat to target."""
        self._clear_notification_backlog()

    async def _notification_pump_loop(self) -> None:
        """
        Single asyncio task that drains the outbound queue (GIF/photo can take many seconds).
        Avoids APScheduler ``max_instances`` overlap with a 1s repeating job.
        """
        ctx = SimpleNamespace(bot=self.app.bot)
        lock = self._notification_drain_lock
        if lock is None:
            self.logger.error("Notification pump started without lock")
            return
        while True:
            try:
                await asyncio.sleep(0.25)
                if self.notification_queue.empty() and not self._requeue_front:
                    continue
                async with lock:
                    await self._drain_notification_queue(ctx)  # type: ignore[arg-type]
            except asyncio.CancelledError:
                break
            except Exception:
                self.logger.exception("Notification pump iteration failed")

    async def _send_notification(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        event: NotificationEvent,
        target_chat_id: int,
    ) -> None:
        """Send a notification."""
        timestamp_str = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")

        if event.photo_path:
            p = Path(event.photo_path)
            if p.is_file():
                if getattr(event, "append_timestamp_footer", True):
                    caption = f"{event.message}\n📅 {timestamp_str}"
                else:
                    caption = event.message
                try:
                    with open(p, "rb") as f:
                        await context.bot.send_photo(
                            chat_id=target_chat_id,
                            photo=f,
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=event.reply_markup,
                            read_timeout=30,
                            write_timeout=60,
                        )
                    self.logger.info("Face photo notification sent")
                except RetryAfter as e:
                    self._merge_telegram_flood_until(float(e.retry_after))
                    raise
                except (TimedOut, NetworkError) as e:
                    self.logger.warning("Network error sending face photo: %s", e)
                    await self._send_text_notification(
                        context,
                        f"{caption}\n<i>(photo send failed)</i>",
                        target_chat_id,
                    )
                except Exception as e:
                    self.logger.error("Error sending face photo: %s", e)
                    await self._send_text_notification(
                        context,
                        f"{caption}\n<i>(photo send failed)</i>",
                        target_chat_id,
                    )
                return

        if event.prefer_plain_text:
            caption = f"{event.message}\n📅 {timestamp_str}"
            await self._send_text_notification(
                context, caption, target_chat_id=target_chat_id
            )
            return

        modes = (
            {"text"}
            if event.event_type == "test"
            else self._modes_for_stage(event.stage)
        )
        if not modes:
            return

        caption = f"<b>{event.message}</b>\n📅 {timestamp_str}"

        has_frames = bool(event.frames and len(event.frames) > 0)
        wants_video = "video" in modes and has_frames
        wants_gif = "gif" in modes and has_frames
        wants_text = "text" in modes

        if wants_video:
            await self._send_video_notification(
                context, event, caption, target_chat_id=target_chat_id
            )
            return
        if wants_gif:
            await self._send_gif_notification(
                context, event, caption, target_chat_id=target_chat_id
            )
            return
        if wants_text or not has_frames:
            await self._send_text_notification(
                context, caption, target_chat_id=target_chat_id
            )
            return

    async def _send_gif_notification(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        event: NotificationEvent,
        caption: str,
        target_chat_id: int,
    ) -> None:
        """Send notification with GIF."""
        try:
            persist_path = None
            if self.media_store and event.camera_id:
                persist_path = self.media_store.new_artifact_path(
                    event.camera_id, event.stage, "gif"
                )
            gif_path = self._create_gif_with_overlay(
                event.frames, event.overlays, out_path=persist_path
            )

            if not gif_path or not gif_path.exists():
                await self._send_text_notification(
                    context, caption, target_chat_id=target_chat_id
                )
                return

            # Check file size
            file_size_mb = gif_path.stat().st_size / (1024 * 1024)
            if file_size_mb > self.max_file_size_mb:
                self.logger.warning(
                    f"GIF too large ({file_size_mb:.1f}MB), sending text only"
                )
                await self._send_text_notification(
                    context, caption, target_chat_id=target_chat_id
                )
                gif_path.unlink(missing_ok=True)
                return

            self._index_notification_media(
                gif_path, event.camera_id, event.stage, "gif"
            )

            # Send GIF
            with open(gif_path, "rb") as f:
                await context.bot.send_animation(
                    chat_id=target_chat_id,
                    animation=f,
                    caption=caption,
                    parse_mode="HTML",
                    read_timeout=30,
                    write_timeout=60,
                )

            if persist_path is None:
                gif_path.unlink(missing_ok=True)
            self.logger.info(f"GIF notification sent: {event.message}")

        except RetryAfter as e:
            with suppress(Exception):
                if persist_path is None and gif_path is not None:
                    Path(gif_path).unlink(missing_ok=True)
            self._merge_telegram_flood_until(float(e.retry_after))
            raise
        except (TimedOut, NetworkError) as e:
            self.logger.warning(f"Network error sending GIF: {e}")
            await self._send_text_notification(
                context, caption, target_chat_id=target_chat_id
            )
        except Exception as e:
            self.logger.error(f"Error sending GIF notification: {e}")
            await self._send_text_notification(
                context, caption, target_chat_id=target_chat_id
            )

    def _index_notification_media(
        self,
        path: Path,
        camera_id: Optional[str],
        stage: str,
        kind: str,
    ) -> None:
        if not camera_id or not self.media_store or not self.memory_manager:
            return
        rel = self.media_store.path_relative_to_root(path)
        if not rel:
            return
        try:
            sz = path.stat().st_size
        except OSError:
            sz = None
        self.memory_manager.insert_media_artifact(
            camera_id=camera_id,
            stage=stage,
            kind=kind,
            path_rel=rel,
            size_bytes=sz,
            metadata={"source": "telegram_notification"},
        )

    def _video_extension_and_fourcc(self) -> Tuple[str, int]:
        fmt = (self.video_format or "mp4").lower().strip()
        if fmt == "avi":
            return ".avi", cv2.VideoWriter_fourcc(*"XVID")
        if fmt not in ("mp4", "mkv"):
            self.logger.warning("Unknown video format %r, using mp4", fmt)
        return ".mp4", cv2.VideoWriter_fourcc(*"mp4v")

    async def _send_video_notification(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        event: NotificationEvent,
        caption: str,
        target_chat_id: int,
    ) -> None:
        """Send notification with a short video clip."""
        try:
            ext, _ = self._video_extension_and_fourcc()
            kind = "mp4" if ext == ".mp4" else "avi"
            persist_path = None
            if self.media_store and event.camera_id:
                persist_path = self.media_store.new_artifact_path(
                    event.camera_id, event.stage, kind
                )
            video_path = self._create_video_with_overlay(
                event.frames, event.overlays, out_path=persist_path
            )
            if not video_path or not video_path.exists():
                await self._send_text_notification(
                    context, caption, target_chat_id=target_chat_id
                )
                return
            file_size_mb = video_path.stat().st_size / (1024 * 1024)
            if file_size_mb > self.max_file_size_mb:
                self.logger.warning(
                    "Video too large (%.1fMB), sending text only", file_size_mb
                )
                await self._send_text_notification(
                    context, caption, target_chat_id=target_chat_id
                )
                video_path.unlink(missing_ok=True)
                return
            self._index_notification_media(
                video_path, event.camera_id, event.stage, kind
            )
            with open(video_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=target_chat_id,
                    video=f,
                    caption=caption,
                    parse_mode="HTML",
                    read_timeout=30,
                    write_timeout=120,
                )
            if persist_path is None:
                video_path.unlink(missing_ok=True)
            self.logger.info("Video notification sent: %s", event.message)
        except RetryAfter as e:
            with suppress(Exception):
                if persist_path is None and video_path is not None:
                    Path(video_path).unlink(missing_ok=True)
            self._merge_telegram_flood_until(float(e.retry_after))
            raise
        except (TimedOut, NetworkError) as e:
            self.logger.warning("Network error sending video: %s", e)
            await self._send_text_notification(
                context, caption, target_chat_id=target_chat_id
            )
        except Exception as e:
            self.logger.error("Error sending video notification: %s", e)
            await self._send_text_notification(
                context, caption, target_chat_id=target_chat_id
            )

    def _create_video_with_overlay(
        self,
        frames: List,
        overlays: Optional[List] = None,
        out_path: Optional[Path] = None,
    ) -> Optional[Path]:
        """Encode sampled frames (with overlays) to a video file (persisted or temp)."""
        try:
            if not frames or len(frames) == 0:
                return None
            if isinstance(frames[0], dict) and "frame" in frames[0]:
                frame_arrays = [f["frame"] for f in frames]
            else:
                frame_arrays = frames

            max_frames = int(self.video_duration * self.video_fps)
            frame_indices = list(range(len(frame_arrays)))
            if len(frame_arrays) > max_frames:
                step = len(frame_arrays) / max_frames
                frame_indices = [int(i * step) for i in range(max_frames)]
                frame_arrays = [frame_arrays[i] for i in frame_indices]

            sampled_overlays: Optional[List] = None
            if overlays and len(overlays) > 0:
                sampled_overlays = []
                for idx in frame_indices:
                    if idx < len(overlays):
                        sampled_overlays.append(overlays[idx])
                    elif len(overlays) > 0:
                        sampled_overlays.append(overlays[-1])
                    else:
                        sampled_overlays.append(None)

            video_frames: List = []
            for i, frame in enumerate(frame_arrays):
                bgr = frame
                if len(bgr.shape) == 3 and bgr.shape[2] == 3:
                    bgr = bgr.copy()
                if sampled_overlays and i < len(sampled_overlays):
                    overlay = sampled_overlays[i]
                    if (
                        overlay is not None
                        and overlay.size > 0
                        and len(overlay.shape) == 3
                    ):
                        ov = overlay
                        if ov.shape[:2] != bgr.shape[:2]:
                            ov = cv2.resize(ov, (bgr.shape[1], bgr.shape[0]))
                        mask = ov.sum(axis=2) > 0
                        bgr[mask] = ov[mask]
                h, w = bgr.shape[:2]
                if w > 1280:
                    scale = 1280 / w
                    new_size = (int(w * scale), int(h * scale))
                    bgr = cv2.resize(bgr, new_size)
                video_frames.append(bgr)

            if not video_frames:
                return None

            ext, fourcc = self._video_extension_and_fourcc()
            if out_path is not None:
                out_path = Path(out_path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
            else:
                out_path = Path(
                    f"temp_vid_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
                )
            h, w = video_frames[0].shape[:2]
            writer = cv2.VideoWriter(
                str(out_path), fourcc, float(self.video_fps), (w, h)
            )
            if not writer.isOpened():
                self.logger.error("VideoWriter failed to open for %s", out_path)
                return None
            for fr in video_frames:
                writer.write(fr)
            writer.release()
            return out_path
        except Exception as e:
            self.logger.error("Error creating video: %s", e, exc_info=True)
            return None

    def _create_gif_with_overlay(
        self,
        frames: List,
        overlays: Optional[List] = None,
        out_path: Optional[Path] = None,
    ) -> Optional[Path]:
        """
        Create GIF from frames with overlays.

        Args:
            frames: List of frame dictionaries with 'frame' key
            overlays: Optional list of overlay arrays

        Returns:
            Path to created GIF file or None
        """
        try:
            if not frames or len(frames) == 0:
                return None

            # Extract frames
            frame_arrays = []
            if isinstance(frames[0], dict) and "frame" in frames[0]:
                frame_arrays = [f["frame"] for f in frames]
            else:
                frame_arrays = frames

            # Limit frames based on duration and FPS
            max_frames = int(self.gif_duration * self.gif_fps)
            frame_indices = list(range(len(frame_arrays)))
            if len(frame_arrays) > max_frames:
                # Sample evenly
                step = len(frame_arrays) / max_frames
                frame_indices = [int(i * step) for i in range(max_frames)]
                frame_arrays = [frame_arrays[i] for i in frame_indices]

            # Sample overlays to match frame indices if available
            sampled_overlays = None
            if overlays and len(overlays) > 0:
                sampled_overlays = []
                for idx in frame_indices:
                    if idx < len(overlays):
                        sampled_overlays.append(overlays[idx])
                    elif len(overlays) > 0:
                        # Use last overlay if index is out of range
                        sampled_overlays.append(overlays[-1])
                    else:
                        sampled_overlays.append(None)

            # Apply overlays if available
            gif_frames = []
            for i, frame in enumerate(frame_arrays):
                # Convert BGR to RGB
                if len(frame.shape) == 3 and frame.shape[2] == 3:
                    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                else:
                    rgb_frame = frame

                # Apply overlay if available
                if sampled_overlays and i < len(sampled_overlays):
                    overlay = sampled_overlays[i]
                    if overlay is not None and overlay.size > 0:
                        # Blend overlay (assuming overlay is BGR)
                        if len(overlay.shape) == 3:
                            overlay_rgb = cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)
                        else:
                            overlay_rgb = overlay

                        # Resize overlay to match frame if needed
                        if overlay_rgb.shape[:2] != rgb_frame.shape[:2]:
                            overlay_rgb = cv2.resize(
                                overlay_rgb, (rgb_frame.shape[1], rgb_frame.shape[0])
                            )

                        # Blend: overlay non-black pixels
                        mask = overlay_rgb.sum(axis=2) > 0
                        rgb_frame[mask] = overlay_rgb[mask]

                # Resize for GIF optimization
                h, w = rgb_frame.shape[:2]
                if w > 1280:
                    scale = 1280 / w
                    new_size = (int(w * scale), int(h * scale))
                    rgb_frame = cv2.resize(rgb_frame, new_size)

                gif_frames.append(rgb_frame)

            if out_path is not None:
                gif_path = Path(out_path)
                gif_path.parent.mkdir(parents=True, exist_ok=True)
            else:
                gif_path = Path(f"temp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gif")

            # Generate GIF
            imageio.mimsave(
                str(gif_path), gif_frames, format="GIF", fps=self.gif_fps, loop=0
            )

            return gif_path

        except Exception as e:
            self.logger.error(f"Error creating GIF: {e}", exc_info=True)
            return None

    async def _send_text_notification(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        caption: str,
        target_chat_id: int,
    ) -> None:
        """Send text-only notification."""
        try:
            await context.bot.send_message(
                chat_id=target_chat_id, text=caption, parse_mode="HTML"
            )
        except RetryAfter as e:
            self._merge_telegram_flood_until(float(e.retry_after))
            raise

    @staticmethod
    def _overlay_row(data_list: List, n: int) -> List:
        row = []
        for i in range(n):
            if i < len(data_list) and isinstance(data_list[i], dict):
                o = data_list[i].get("overlay")
                row.append(o if o is not None else None)
            else:
                row.append(None)
        return row

    @staticmethod
    def _copy_buffer(arr: Any) -> Any:
        """Deep-copy numpy frame/overlay so async GIF/video send survives rolling-buffer reuse."""
        if arr is None:
            return None
        if isinstance(arr, np.ndarray):
            return np.copy(arr)
        copy_fn = getattr(arr, "copy", None)
        if callable(copy_fn):
            try:
                return copy_fn()
            except Exception:
                return arr
        return arr

    def _snapshot_notification_frames(
        self, frames_to_use: List[Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """Copy camera frames for the notification queue (see CamGrabber.record() — refs only)."""
        if not frames_to_use:
            return None
        out: List[Dict[str, Any]] = []
        for f in frames_to_use:
            if f is None:
                continue
            out.append({"frame": self._copy_buffer(f)})
        return out if out else None

    def _snapshot_overlays(self, overlays: Optional[List[Any]]) -> Optional[List[Any]]:
        if not overlays:
            return overlays
        return [self._copy_buffer(o) for o in overlays]

    def process(self, result: Dict[str, Any]) -> None:
        """
        Process result from orchestrator.

        Args:
            result: Result dictionary from orchestrator
        """
        camera_id = result.get("camera_id", "unknown")
        face_block = result.get("face") or {}
        face_alarmed = bool(face_block.get("alarmed", False))
        detection_alarmed = result.get("detection", {}).get("alarmed", False)
        peak_alarmed = result.get("peak", {}).get("alarmed", False)
        motion_alarmed = result.get("motion", {}).get("alarmed", False)
        preproc_alarmed = peak_alarmed or motion_alarmed

        if face_alarmed and self._modes_for_stage("face"):
            face_data = face_block.get("data")
            structured = (
                isinstance(face_data, dict)
                and isinstance(face_data.get("faces"), list)
                and len(face_data["faces"]) > 0
            )
            if structured:
                faces = face_data["faces"]
                known_names: List[str] = []
                first_unknown: Optional[Dict[str, Any]] = None
                for f in faces:
                    if not isinstance(f, dict):
                        continue
                    if f.get("notification_hint") == "known_text" and f.get(
                        "display_name"
                    ):
                        known_names.append(str(f["display_name"]))
                    elif (
                        f.get("notification_hint") == "unknown_prompt"
                        and first_unknown is None
                    ):
                        first_unknown = f
                if known_names:
                    if len(known_names) == 1:
                        msg = f"🙂 <b>{known_names[0]}</b> is here · <code>{camera_id}</code>"
                    else:
                        msg = (
                            "🙂 <b>"
                            + ", ".join(known_names)
                            + f"</b> are here · <code>{camera_id}</code>"
                        )
                    self._queue_notification(
                        NotificationEvent(
                            message=msg,
                            event_type="face",
                            stage="face",
                            camera_id=camera_id,
                            prefer_plain_text=True,
                        )
                    )
                if first_unknown:
                    crop = first_unknown.get("crop_path")
                    pid = str(first_unknown.get("pending_face_id") or "")
                    if crop and Path(crop).is_file():
                        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                        ref_lower, ref_disp = ("", "")
                        if pid:
                            ref_lower, ref_disp = self._register_face_pending_ref(pid)
                        markup = (
                            self._unknown_face_reply_markup(ref_lower)
                            if ref_lower
                            else None
                        )
                        if pid and ref_disp:
                            cap = (
                                "<b>Unknown face</b>\n"
                                f"Camera: <code>{camera_id}</code>\n"
                                f"Ref: <code>{ref_disp}</code>\n"
                                "Pending: <b>open</b>\n"
                                f"{ts}\n"
                                f"<i>Buttons below, or</i> <code>/fa {ref_disp} NewName</code>"
                            )
                        elif pid:
                            cap = (
                                "<b>Unknown face</b>\n"
                                f"Camera: <code>{camera_id}</code>\n"
                                "Pending: <b>open</b>\n"
                                f"{ts}\n"
                                f"<code>/face_assign {pid} NewName</code> · "
                                f"<code>/face_ignore {pid}</code>"
                            )
                        else:
                            cap = (
                                "<b>Unknown face</b>\n"
                                f"Camera: <code>{camera_id}</code>\n"
                                "Pending: <b>open</b>\n"
                                f"{ts}\n"
                                "<i>No pending id (DB write failed?). Use the dashboard or logs.</i>"
                            )
                        self._queue_notification(
                            NotificationEvent(
                                message=cap,
                                event_type="face",
                                stage="face",
                                camera_id=camera_id,
                                photo_path=str(Path(crop)),
                                reply_markup=markup,
                                append_timestamp_footer=False,
                            )
                        )
                if not known_names and not first_unknown:
                    structured = False
            if not structured:
                modes = self._modes_for_stage("face")
                face_data = face_block.get("data")
                data_list = face_data if isinstance(face_data, list) else []
                record_frames = result.get("record", [])
                max_frames = self._max_frames_for_modes(modes)
                frames_to_use = record_frames[:max_frames] if record_frames else []
                if not frames_to_use:
                    frames_to_use = (
                        [result.get("snap")] if result.get("snap") is not None else []
                    )
                frames = self._snapshot_notification_frames(frames_to_use)
                overlays = self._snapshot_overlays(
                    self._overlay_row(data_list, len(frames_to_use))
                )
                event = NotificationEvent(
                    message=f"🙂 Face identified on camera {camera_id}",
                    event_type="face",
                    stage="face",
                    camera_id=camera_id,
                    frames=frames,
                    overlays=overlays,
                )
                self._queue_notification(event)

        elif detection_alarmed and self.notify_detection:
            modes = self.notify_detection
            detection_data = result.get("detection", {}).get("data", [])
            record_frames = result.get("record", [])
            max_frames = self._max_frames_for_modes(modes)
            frames_to_use = record_frames[:max_frames] if record_frames else []
            if not frames_to_use:
                frames_to_use = (
                    [result.get("snap")] if result.get("snap") is not None else []
                )
            frames = self._snapshot_notification_frames(frames_to_use)
            overlays = self._snapshot_overlays(
                self._overlay_row(detection_data, len(frames_to_use))
            )
            event = NotificationEvent(
                message=f"🚨 Person detected on camera {camera_id}!",
                event_type="person",
                stage="detection",
                camera_id=camera_id,
                frames=frames,
                overlays=overlays,
            )
            self._queue_notification(event)

        elif (
            preproc_alarmed
            and not detection_alarmed
            and not face_alarmed
            and self.notify_preproc
        ):
            modes = self.notify_preproc
            motion_data = result.get("motion", {}).get("data", [])
            record_frames = result.get("record", [])
            max_frames = self._max_frames_for_modes(modes)
            frames_to_use = record_frames[:max_frames] if record_frames else []
            if not frames_to_use:
                frames_to_use = (
                    [result.get("snap")] if result.get("snap") is not None else []
                )
            frames = self._snapshot_notification_frames(frames_to_use)
            overlays = self._snapshot_overlays(
                self._overlay_row(motion_data, len(frames_to_use))
            )
            event = NotificationEvent(
                message=f"👀 Motion detected on camera {camera_id}",
                event_type="motion",
                stage="preproc",
                camera_id=camera_id,
                frames=frames,
                overlays=overlays,
            )
            self._queue_notification(event)

    async def start(self) -> None:
        """Start the Telegram bot."""
        try:
            from ..logging_redact import install_telegram_token_log_redaction

            install_telegram_token_log_redaction()

            self.logger.info("Starting Telegram bot...")

            await self.app.initialize()
            await self.app.start()
            self._notification_drain_lock = asyncio.Lock()
            # One asyncio task drains the queue (GIF/photo can block many seconds; avoids
            # APScheduler ``max_instances`` warnings from a 1s repeating job).
            self._notification_pump_task = asyncio.create_task(
                self._notification_pump_loop(),
                name="telegram_notification_pump",
            )
            self.app.job_queue.run_repeating(
                self._pause_reminder_tick,
                interval=60.0,
                first=120.0,
            )

            # Start polling
            await self.app.updater.start_polling(drop_pending_updates=True)

            self.logger.info("Telegram bot started successfully")

            # Update service status
            if self.memory_manager:
                self.memory_manager.update_service_status(
                    "telegram_bot", is_running=True
                )

        except Exception as e:
            self.logger.error(f"Failed to start bot: {e}", exc_info=True)
            raise

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        try:
            self.logger.info("Stopping Telegram bot...")
            if self._notification_pump_task:
                self._notification_pump_task.cancel()
                try:
                    await self._notification_pump_task
                except asyncio.CancelledError:
                    pass
                self._notification_pump_task = None

            if self.app.updater.running:
                await self.app.updater.stop()

            await self.app.stop()
            await self.app.shutdown()

            # Update service status
            if self.memory_manager:
                self.memory_manager.update_service_status(
                    "telegram_bot", is_running=False
                )

            self.logger.info("Telegram bot stopped successfully")

        except Exception as e:
            self.logger.error(f"Error stopping bot: {e}", exc_info=True)

    def run(self) -> None:
        """Run the Telegram bot in an event loop (synchronous wrapper for threading)."""
        try:
            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Run the async start method
            loop.run_until_complete(self.start())

            # Keep the loop running
            try:
                loop.run_forever()
            except KeyboardInterrupt:
                self.logger.info("Telegram bot interrupted")
            finally:
                loop.run_until_complete(self.stop())
                loop.close()

        except Exception as e:
            self.logger.error(f"Error running Telegram bot: {e}", exc_info=True)
            raise
