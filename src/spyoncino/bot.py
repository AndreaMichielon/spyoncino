"""
Security Telegram Bot Module

Professional Telegram bot providing security system control, notifications,
and user management with authorization, rate limiting, and rich command interface.
"""

import asyncio
import io 
import queue
import logging
import cv2
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Callable, Union
from dataclasses import dataclass, field, asdict
from functools import wraps
from contextlib import contextmanager

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import TelegramError, TimedOut, NetworkError

from spyoncino.manager import SecurityEventManager  # Import the improved SecurityEventManager


@dataclass
class BotConfig:
    """Bot configuration settings."""
    gif_for_motion: bool = False
    gif_for_person: bool = True
    gif_duration: int = 3
    gif_fps: int = 10
    max_file_size_mb: float = 50.0
    notification_rate_limit: int = 5
    
    # Security settings
    user_whitelist: List[int] = field(default_factory=list)
    superuser_id: Optional[int] = None
    setup_password: Optional[str] = None 

    # Multi-user settings
    notification_chat_id: Optional[int] = None  # Dedicated chat for alerts
    allow_group_commands: bool = True           # Allow commands in groups
    silent_unauthorized: bool = True            # Silent rejection in groups

@dataclass
class NotificationEvent:
    """Represents a notification event."""
    message: str
    event_file: Optional[str] = None
    timestamp: datetime = None
    event_type: str = "unknown"
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

@contextmanager
def _temp_file(path: Path, logger=None):
    """
    Context manager for temporary files that ensures cleanup.
    
    Args:
        path: Path to the temporary file
        logger: Optional logger for logging cleanup events
        
    Yields:
        Path: The path to the temporary file
        
    Ensures:
        File is deleted after use, even if exceptions occur
    """
    try:
        yield path
    finally:
        if path.exists():
            try:
                path.unlink()
                if logger:
                    logger.debug(f"Temporary file cleaned up: {path}")
            except OSError as e:
                if logger:
                    logger.warning(f"Failed to cleanup temporary file {path}: {e}")

def _require_authorization(func):
    """Decorator for command handlers requiring authorization."""
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            await update.message.reply_text("❌ Unable to identify user.")
            return
        
        # Check if group commands are disabled
        if self._is_group_context(update) and not self.config.allow_group_commands:
            return  # Silent ignore in groups if disabled
            
        if not self._is_authorized_user(update.effective_user.id):
            await self._unauthorized_response(update)
            return
        return await func(self, update, context)
    return wrapper

def _require_superuser(func):
    """Decorator for command handlers requiring superuser access."""
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            await update.message.reply_text("❌ Unable to identify user.")
            return
            
        if not self._is_superuser(update.effective_user.id):
            await update.message.reply_text("🚫 Superuser access required.")
            return
        return await func(self, update, context)
    return wrapper

class SecurityTelegramBot:
    """
    Professional Telegram bot for security system notifications and control.
    
    Features:
    - Rich command interface with inline keyboards
    - Configurable notification settings
    - Rate limiting and error handling
    - Statistics and monitoring
    - File size optimization
    - Professional logging and debugging
    """
    
    def __init__(
        self,
        token: str,
        event_manager: SecurityEventManager,
        chat_id: Optional[Union[int, str]] = None,
        config: Optional[BotConfig] = None,
    ):
        """
        Initialize the security Telegram bot.
        
        Args:
            token: Telegram bot token
            event_manager: SecurityEventManager instance
            chat_id: Target chat ID (can be set later via first message)
            config: Bot configuration settings
        """
        if not token or not token.strip():
            raise ValueError("token cannot be empty")
        if not isinstance(event_manager, SecurityEventManager):
            raise TypeError("event_manager must be a SecurityEventManager instance")
        if chat_id is not None and not isinstance(chat_id, (int, str)):
            raise TypeError("chat_id must be int, str, or None")

        # Core components
        self.token = token
        self.event_manager = event_manager
        self.chat_id = chat_id
        self.config = config or BotConfig()
        
        # Telegram application
        self.app = Application.builder().token(token).build()
        
        # Authorized users
        self._user_whitelist = self.config.user_whitelist
        self._superuser_id = self.config.superuser_id

        self._failed_attempts = {}  # user_id -> attempt count
        self._setup_password = getattr(config, 'setup_password', None)  # Get from config
        self._pending_setup = {}

        # Notification system
        self.notification_queue = queue.Queue()
        self.notification_stats = {
            'sent': 0,
            'failed': 0,
            'rate_limited': 0,
            'last_notification': None
        }
        
        # Rate limiting
        self._notification_times: List[datetime] = []
        
        # Setup command handlers
        self._setup_command_handlers()
        
        # Setup event handlers
        self._setup_event_handlers()
        
        # Logging
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info("Security Telegram bot initialized")
    
    def _is_private_chat(self, chat_id: Optional[int] = None) -> bool:
        """Check if current or given chat is private."""
        # This would need to be tracked, but for now assume based on context
        return True  # Simplified - in practice you'd track chat types

    def _is_group_context(self, update: Update) -> bool:
        """Check if message is from a group context."""
        return update.effective_chat.type in ['group', 'supergroup']

    def _get_notification_chat_id(self) -> Optional[int]:
        """Determine appropriate chat for security notifications."""
        # Priority: dedicated notification chat > stored chat_id
        if self.config.notification_chat_id:
            return self.config.notification_chat_id
        return self.chat_id

    def _setup_command_handlers(self) -> None:
        """Register all command handlers."""
        handlers = [
            ("setup", self.cmd_setup), 
            ("whoami", self.cmd_whoami),
            ("whitelist_add", self.cmd_whitelist_add),
            ("whitelist_remove", self.cmd_whitelist_remove), 
            ("whitelist_list", self.cmd_whitelist_list),
            ("start", self.cmd_start_help),
            ("help", self.cmd_help),
            ("status", self.cmd_status),
            ("stats", self.cmd_statistics),
            ("recordings", self.cmd_recordings),
            ("get", self.cmd_get_recording),
            ("snap", self.cmd_snapshot),
            ("start_monitor", self.cmd_start_monitoring),
            ("stop_monitor", self.cmd_stop_monitoring),
            ("config", self.cmd_configure),
            ("show_config", self.cmd_show_config),
            ("cleanup", self.cmd_force_cleanup),
            ("test", self.cmd_test_notification),
            ("timeline", self.cmd_timeline),
            ("analytics", self.cmd_analytics),
        ]
        
        for command, handler in handlers:
            self.app.add_handler(CommandHandler(command, handler))
        
        # Callback query handler for inline keyboards
        self.app.add_handler(CallbackQueryHandler(self.handle_callback_query))
    
    def _setup_event_handlers(self) -> None:
        """Setup event handlers for the security system."""
        # Override the event manager's handlers to use our notification system
        self.event_manager.on_motion = self._handle_motion_event
        self.event_manager.on_person = self._handle_person_event
        self.event_manager.on_disconnect = self._handle_disconnect_event
        self.event_manager.on_storage_warning = self._handle_storage_warning
    
    def _handle_motion_event(self, event_file: str) -> None:
        """Handle motion detection events."""
        # File is guaranteed to exist at this point
        self.logger.debug(f"Motion event received - file: {event_file}, exists: {Path(event_file).exists()}")
        
        event = NotificationEvent(
            message="👀 Motion detected",
            event_file=event_file,
            event_type="motion"
        )
        self._queue_notification(event)

    def _handle_person_event(self, event_file: str) -> None:
        """Handle person detection events."""
        # File is guaranteed to exist at this point
        self.logger.debug(f"Person event received - file: {event_file}, exists: {Path(event_file).exists()}")
        
        event = NotificationEvent(
            message="🚨 Person detected!",
            event_file=event_file,
            event_type="person"
        )
        self._queue_notification(event)
    
    def _handle_disconnect_event(self) -> None:
        """Handle capture disconnection events."""
        event = NotificationEvent(
            message="⚠️ Capture disconnected!",
            event_type="disconnect"
        )
        self._queue_notification(event)
    
    def _handle_storage_warning(self, storage_info) -> None:
        """Handle low storage warnings."""
        event = NotificationEvent(
            message=f"💾 Low storage warning: {storage_info.free_gb:.1f}GB free",
            event_type="storage"
        )
        self._queue_notification(event)
    
    def _queue_notification(self, event: NotificationEvent) -> None:
        """Queue a notification event for processing."""
        if not isinstance(event, NotificationEvent):
            self.logger.error("Invalid notification event type")
            return

        if self._is_rate_limited():
            self.notification_stats['rate_limited'] += 1
            self.logger.warning("Notification rate limited")
            return
        
        self.notification_queue.put(event)
        self.logger.debug(f"Queued notification: {event.message}")
    
    def _is_rate_limited(self) -> bool:
        """Check if we're hitting rate limits."""
        now = datetime.now()
        # Remove notifications older than 1 minute
        self._notification_times = [
            t for t in self._notification_times 
            if (now - t).total_seconds() < 60
        ]
        
        return len(self._notification_times) >= self.config.notification_rate_limit
    
    def _update_chat_id(self, update: Update) -> None:
        """Update chat_id from incoming message if not set."""
        if not self.chat_id and update.effective_chat:
            self.chat_id = update.effective_chat.id
            chat_type = "private" if not self._is_group_context(update) else "group"
            self.logger.info(f"Chat ID set to {chat_type} chat: {self.chat_id}")
            
            # Warn if using group for notifications without dedicated notification chat
            if self._is_group_context(update) and not self.config.notification_chat_id:
                self.logger.warning("Using group chat for notifications - consider setting notification_chat_id")
    
    async def _process_notification_queue(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process queued notifications and send them."""
        try:
            while not self.notification_queue.empty():
                event = self.notification_queue.get()
                
                notification_chat = self._get_notification_chat_id()
                if not notification_chat:
                    self.logger.warning("No notification chat configured - notification dropped")
                    continue
                
                try:
                    await self._send_notification(context, event)
                    self.notification_stats['sent'] += 1
                    self.notification_stats['last_notification'] = datetime.now()
                    self._notification_times.append(datetime.now())
                    
                except Exception as e:
                    self.notification_stats['failed'] += 1
                    self.logger.error(f"Failed to send notification: {e}", exc_info=True)
                    
        except (asyncio.TimeoutError, queue.Empty) as e:
            self.logger.debug(f"Notification queue timeout: {e}")
        except Exception as e:
            self.logger.error(f"Error processing notification queue: {e}", exc_info=True)
    
    async def _send_notification(self, context: ContextTypes.DEFAULT_TYPE, event: NotificationEvent) -> None:
        """Send a single notification."""
        try:
            timestamp_str = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            
            # Determine if we should send media
            should_send_media = (
                event.event_file and 
                Path(event.event_file).exists() and
                ((event.event_type == "person" and self.config.gif_for_person) or
                (event.event_type == "motion" and self.config.gif_for_motion))
            )
            
            if should_send_media:
                await self._send_media_notification(context, event, timestamp_str)
            else:
                await self._send_text_notification(context, event, timestamp_str)
        
        except Exception as e:
            self.notification_stats['failed'] += 1
            self.logger.error(f"Failed to send notification: {e}", exc_info=True)
            
            # Log to event system if available
            if hasattr(self.event_manager, 'event_logger'):
                from analytics import SecurityEvent, EventType
                self.event_manager.event_logger.log_event(SecurityEvent(
                    timestamp=datetime.now(),
                    event_type=EventType.ERROR,
                    message=f"Notification failed: {str(e)[:100]}",
                    severity="error"
                ))

    async def _send_media_notification(
        self, 
        context: ContextTypes.DEFAULT_TYPE, 
        event: NotificationEvent, 
        timestamp_str: str
    ) -> None:
        """Send notification with media attachment."""
        try:
            caption = f"<b>{event.message}</b>\n📅 {timestamp_str}"
            
            # Check file size
            file_path = Path(event.event_file)
            file_size_mb = file_path.stat().st_size / (1024 * 1024)
            
            if file_size_mb > self.config.max_file_size_mb:
                self.logger.warning(f"File too large ({file_size_mb:.1f}MB), sending text only")
                await self._send_text_notification(context, event, timestamp_str)
                return
            
            with open(event.event_file, "rb") as f:
                await context.bot.send_animation(
                    chat_id=self._get_notification_chat_id(),
                    animation=f,
                    caption=caption,
                    parse_mode='HTML',
                    read_timeout=30,
                    write_timeout=60
                )
                
            self.logger.info(f"Media notification sent: {event.message}")
            
        except (TimedOut, NetworkError) as e:
            self.logger.warning(f"Network error sending media: {e}")
            await self._send_text_notification(context, event, timestamp_str)
        except (TelegramError, OSError, IOError) as e:
            self.logger.error(f"Error sending media notification: {e}")
            raise
    
    async def _send_text_notification(
        self, 
        context: ContextTypes.DEFAULT_TYPE, 
        event: NotificationEvent, 
        timestamp_str: str
    ) -> None:
        """Send text-only notification."""
        caption = f"<b>{event.message}</b>\n📅 {timestamp_str}"
        
        await context.bot.send_message(
            chat_id=self.chat_id,
            text=caption,
            parse_mode='HTML'
        )
        
        self.logger.info(f"Text notification sent: {event.message}")
    
    # Authorization 
    def _is_authorized_user(self, user_id: int) -> bool:
        """Check if user is authorized with rate limiting and setup support."""
        if not isinstance(user_id, int) or user_id <= 0:
            return False
        
        # Check rate limiting
        if self._is_rate_limited_user(user_id):
            return False
        
        # If no superuser set and setup password exists, allow setup process
        if not self._superuser_id and self._setup_password:
            return True  # Allow setup commands
        
        # Check if user is superuser
        if self._is_superuser(user_id):
            return True
        
        # Check whitelist
        if not self._user_whitelist:
            return True  # No whitelist = open access
        
        return user_id in self._user_whitelist

    async def _unauthorized_response(self, update: Update) -> None:
        """Enhanced unauthorized response with rate limiting."""
        user_id = update.effective_user.id
        
        if self._is_rate_limited_user(user_id):
            if not self._is_group_context(update):
                await update.message.reply_text("🚫 Too many failed attempts. Access temporarily blocked.")
            return
        
        self._record_failed_attempt(user_id)
        
        # Existing unauthorized response logic
        self.logger.warning(f"Unauthorized access attempt from user {user_id} ({update.effective_user.full_name})")
        
        if self._is_group_context(update) and self.config.silent_unauthorized:
            return
        elif self._is_group_context(update):
            await update.message.reply_text("🚫 Access denied.", reply_to_message_id=update.message.message_id)
        else:
            await update.message.reply_text(f"🚫 Unauthorized access - User: {user_id}.")

    def _is_superuser(self, user_id: int) -> bool:
        """Check if user is the configured superuser."""
        if not isinstance(user_id, int) or user_id <= 0:
            return False
        return self._superuser_id and user_id == self._superuser_id

    # Command Handlers
    async def cmd_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle first-time setup with password verification."""
        user_id = update.effective_user.id
        
        # Only allow if no superuser is set
        if self._superuser_id:
            await update.message.reply_text("🔒 System already configured.")
            return
        
        # Check rate limiting
        if self._is_rate_limited_user(user_id):
            await update.message.reply_text("🚫 Too many failed attempts. Try again later.")
            return
        
        # Check if setup password is required
        if not self._setup_password:
            # No password required, make first user superuser
            self._superuser_id = user_id
            self._user_whitelist = [user_id]
            self._reset_failed_attempts(user_id)
            self.logger.info(f"First user {user_id} became superuser (no password required)")
            await update.message.reply_text(
                "👑 <b>Welcome, Superuser!</b>\n\n"
                "You are now the system administrator.\n"
                "Use /help to see all available commands.",
                parse_mode='HTML'
            )
            return
        
        # Password required
        if not context.args:
            await update.message.reply_text(
                "🔐 <b>First-Time Setup</b>\n\n"
                "Enter the setup password:\n"
                "<code>/setup [password]</code>\n\n"
                "💡 <i>The password was configured when the system was installed.</i>",
                parse_mode='HTML'
            )
            return
        
        password = self._sanitize_input(" ".join(context.args), 50)
        
        if password == self._setup_password:
            self._superuser_id = user_id
            self._user_whitelist = [user_id]
            self._reset_failed_attempts(user_id)
            
            self.logger.info(f"First-time setup completed by user {user_id}")
            await update.message.reply_text(
                "✅ <b>Setup Complete!</b>\n\n"
                "You are now the system superuser.\n"
                "Use /help to see all available commands.",
                parse_mode='HTML'
            )
        else:
            self._record_failed_attempt(user_id)
            attempts_left = max(0, 5 - self._failed_attempts.get(user_id, 0))
            
            self.logger.warning(f"Failed setup attempt by user {user_id}")
            await update.message.reply_text(
                f"❌ Invalid setup password.\n"
                f"⚠️ {attempts_left} attempts remaining before temporary lockout."
            )

    async def cmd_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show current user information."""
        user = update.effective_user
        
        # Show different info based on authorization status
        if self._is_authorized_user(user.id):
            status = '👑 Superuser' if self._is_superuser(user.id) else '✅ Authorized'
        else:
            status = '🚫 Unauthorized'
        
        user_info = (
            f"👤 <b>User Information</b>\n\n"
            f"• ID: <code>{user.id}</code>\n"
            f"• Username: @{user.username or 'None'}\n"
            f"• Name: {user.full_name}\n"
            f"• Status: {status}"
        )
        
        if not self._is_authorized_user(user.id):
            user_info += "\n\n💡 <i>Send your ID to the admin to request access.</i>"
        
        await update.message.reply_text(user_info, parse_mode='HTML')

    def _sanitize_input(self, text: str, max_length: int = 100) -> str:
        """Sanitize user input to prevent injection attacks."""
        if not text:
            return ""
        sanitized = text.strip()[:max_length]
        dangerous_chars = [';', '&', '|', '`', '$', '(', ')', '{', '}', '[', ']']
        for char in dangerous_chars:
            sanitized = sanitized.replace(char, '')
        return sanitized
    
    def _validate_numeric_arg(
        self, 
        args: List[str], 
        arg_name: str, 
        min_val: Optional[float] = None, 
        max_val: Optional[float] = None,
        as_int: bool = False
    ) -> tuple[Optional[Union[int, float]], Optional[str]]:
        """
        Validate numeric argument with clear error message.
        
        Args:
            args: Command arguments list
            arg_name: Name of argument for error messages
            min_val: Minimum allowed value (inclusive)
            max_val: Maximum allowed value (inclusive)
            as_int: Return as integer instead of float
            
        Returns:
            Tuple of (value, error_message). If valid, error_message is None.
        """
        if not args:
            return None, f"❌ Missing {arg_name}"
        try:
            val = int(args[0]) if as_int else float(args[0])
            if min_val is not None and val < min_val:
                return None, f"❌ {arg_name} must be >= {min_val}"
            if max_val is not None and val > max_val:
                return None, f"❌ {arg_name} must be <= {max_val}"
            return val, None
        except (ValueError, IndexError):
            return None, f"❌ Invalid {arg_name} (must be a {'number' if not as_int else 'whole number'})"

    def _is_rate_limited_user(self, user_id: int) -> bool:
        """Check if user is rate limited due to failed attempts."""
        return self._failed_attempts.get(user_id, 0) >= 5

    def _record_failed_attempt(self, user_id: int) -> None:
        """Record a failed authentication attempt."""
        self._failed_attempts[user_id] = self._failed_attempts.get(user_id, 0) + 1
        
    def _reset_failed_attempts(self, user_id: int) -> None:
        """Reset failed attempts for a user."""
        self._failed_attempts.pop(user_id, None)

    @_require_superuser
    async def cmd_whitelist_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Add user to whitelist (superuser only)."""
        if not context.args:
            await update.message.reply_text("Usage: /whitelist_add <user_id>")
            return
            
        try:
            user_id = int(context.args[0])
            if user_id <= 0:
                await update.message.reply_text("❌ User ID must be positive")
                return

            if user_id not in self._user_whitelist:
                self._user_whitelist.append(user_id)
                self.logger.info(f"Superuser {update.effective_user.id} added user {user_id} to whitelist")
                await update.message.reply_text(f"✅ User {user_id} added to whitelist.")
            else:
                await update.message.reply_text("ℹ️ User already whitelisted.")
        except ValueError:
            self.logger.warning(f"Superuser {update.effective_user.id} provided invalid user ID: {context.args[0]}")
            await update.message.reply_text("❌ Invalid user ID.")

    @_require_superuser
    async def cmd_whitelist_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Remove user from whitelist (superuser only)."""
        if not context.args:
            await update.message.reply_text("Usage: /whitelist_remove <user_id>")
            return
            
        try:
            user_id = int(context.args[0])
            if user_id in self._user_whitelist:
                self._user_whitelist.remove(user_id)
                self.logger.info(f"Superuser {update.effective_user.id} removed user {user_id} from whitelist")
                await update.message.reply_text(f"✅ User {user_id} removed from whitelist.")
            else:
                await update.message.reply_text("ℹ️ User not in whitelist.")
        except ValueError:
            self.logger.warning(f"Superuser {update.effective_user.id} provided invalid user ID: {context.args[0]}")
            await update.message.reply_text("❌ Invalid user ID.")

    @_require_superuser
    async def cmd_whitelist_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show current whitelist (superuser only)."""
        self.logger.info(f"Superuser {update.effective_user.id} requested whitelist")
        
        if not self._user_whitelist:
            await update.message.reply_text("📝 Whitelist is empty (all users allowed).")
        else:
            users_text = "\n".join([f"• {uid}" for uid in self._user_whitelist])
            await update.message.reply_text(f"📝 <b>Whitelisted Users:</b>\n{users_text}", parse_mode='HTML')

    async def cmd_start_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command - show welcome message."""
        # Special handling for unauthorized users
        if not self._is_authorized_user(update.effective_user.id):
            welcome_text = (
                "🤖 <b>Security Bot</b>\n\n"
                "Use /whoami to get your user ID, then contact the admin for access.\n"
                "Once authorized, use /start again to see the full interface."
            )
            await update.message.reply_text(welcome_text, parse_mode='HTML')
            return

        self._update_chat_id(update)
        
        welcome_text = (
            "🤖 <b>Security Bot Activated!</b>\n\n"
            "I'm your AI-powered security assistant. I can monitor your space, "
            "detect motion and people, and send you real-time alerts.\n\n"
            "Use /help to see all available commands."
        )
        
        await update.message.reply_text(welcome_text, parse_mode='HTML')
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show comprehensive help message."""
        # Show basic help for unauthorized users
        if not self._is_authorized_user(update.effective_user.id):
            await update.message.reply_text(
                "🤖 <b>Security Bot</b>\n\n"
                "Available commands:\n"
                "/whoami - Get your user ID\n\n"
                "💡 <i>Contact the admin with your user ID to request access.</i>",
                parse_mode='HTML'
            )
            return

        self._update_chat_id(update)

        base_help = (
            "🤖 <b>Security Bot Commands</b>\n\n"
            
            "<b>👤 Identity Info:</b>\n"
            "/whoami - Show your user info\n\n"

            "<b>📊 System Control:</b>\n"
            "/start - Welcome message\n"
            "/status - Show system status\n"
            "/stats - Detailed statistics\n"
            "/start_monitor - Start monitoring\n"
            "/stop_monitor - Stop monitoring\n\n"
            
            "<b>📹 Recordings:</b>\n"
            "/recordings - Browse recordings\n"
            "/get &lt;index|name&gt; - Get specific recording\n"
            "/snap - Live camera snapshot\n"
            "/cleanup - Force cleanup old files\n\n"
            
            "<b>📊 Analytics:</b>\n"
            "/timeline [hours] - Event timeline plot\n"
            "/analytics [hours] - Analytics summary\n\n"

            "<b>⚙️ Configuration:</b>\n"
            "/show_config - Current settings\n"
            "/config &lt;key&gt; &lt;value&gt; - Change setting\n\n"
            
            "<b>🔧 Debugging:</b>\n"
            "/test - Test notification system\n\n"
            
            "<b>Config Keys:</b>\n"
            "• <code>interval</code> - Check interval (seconds)\n"
            "• <code>frames</code> - Recording length (frames)\n"
            "• <code>gif_motion</code> - GIF for motion (on/off)\n"
            "• <code>gif_person</code> - GIF for person (on/off)\n"
            "• <code>threshold</code> - Motion sensitivity"
        )

        # Add admin commands if user is superuser
        if self._is_superuser(update.effective_user.id):
            admin_help = (
                "\n\n<b>👑 Admin Commands:</b>\n"
                "/whitelist_add &lt;user_id&gt; - Add user to whitelist\n"
                "/whitelist_remove &lt;user_id&gt; - Remove user\n"
                "/whitelist_list - Show whitelisted users"
            )
            base_help += admin_help
        
        await update.message.reply_text(base_help, parse_mode='HTML')
        
    @_require_authorization
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show system status."""
        self._update_chat_id(update)
        
        # Get comprehensive status
        manager_stats = self.event_manager.get_statistics()
        security_status = self.event_manager.security_system.status
        
        status_text = (
            f"🔍 <b>Security System Status</b>\n\n"
            f"<b>Manager:</b> {'✅ Running' if manager_stats['running'] else '❌ Stopped'}\n"
            f"<b>Capture:</b> {'✅ Connected' if security_status['capture_connected'] else '❌ Disconnected'}\n"
            f"<b>GPU:</b> {'✅ Available' if security_status['gpu_available'] else '❌ Not Available'}\n"
            f"<b>Model:</b> {'✅ Loaded' if security_status['model_loaded'] else '❌ Not Loaded'}\n\n"
            
            f"<b>📊 Quick Stats:</b>\n"
            f"• Uptime: {manager_stats['uptime_seconds']//3600:.0f}h {(manager_stats['uptime_seconds']%3600)//60:.0f}m\n"
            f"• Events: {manager_stats['events_processed']}\n"
            f"• Storage: {manager_stats['storage']['usage_percent']:.1f}% used\n"
            f"• Recordings: {manager_stats['total_recordings']}"
        )
        
        await update.message.reply_text(status_text, parse_mode='HTML')
    
    @_require_authorization
    async def cmd_statistics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show detailed statistics."""
        self._update_chat_id(update)
        
        manager_stats = self.event_manager.get_statistics()
        
        uptime_hours = manager_stats['uptime_seconds'] // 3600
        uptime_minutes = (manager_stats['uptime_seconds'] % 3600) // 60
        
        stats_text = (
            f"📈 <b>Detailed Statistics</b>\n\n"
            
            f"<b>⏱️ Runtime:</b>\n"
            f"• Uptime: {uptime_hours}h {uptime_minutes}m\n"
            f"• Started: {manager_stats.get('start_time', 'Unknown')}\n\n"
            
            f"<b>🎯 Detection Events:</b>\n"
            f"• Total Events: {manager_stats['events_processed']}\n"
            f"• Person Events: {manager_stats['person_events']}\n"
            f"• Motion Events: {manager_stats['motion_events']}\n\n"
            
            f"<b>💾 Storage:</b>\n"
            f"• Total Space: {manager_stats['storage']['total_gb']:.1f}GB\n"
            f"• Used: {manager_stats['storage']['used_gb']:.1f}GB\n"
            f"• Free: {manager_stats['storage']['free_gb']:.1f}GB\n"
            f"• Usage: {manager_stats['storage']['usage_percent']:.1f}%\n\n"
            
            f"<b>🗂️ Files:</b>\n"
            f"• Current Recordings: {manager_stats['total_recordings']}\n"
            f"• Files Cleaned: {manager_stats['files_cleaned']}\n\n"
            
            f"<b>📬 Notifications:</b>\n"
            f"• Sent: {self.notification_stats['sent']}\n"
            f"• Failed: {self.notification_stats['failed']}\n"
            f"• Rate Limited: {self.notification_stats['rate_limited']}"
        )
        
        await update.message.reply_text(stats_text, parse_mode='HTML')
    
    @_require_authorization
    async def cmd_start_monitoring(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start security monitoring."""
        self._update_chat_id(update)
        
        if self.event_manager.start():
            await update.message.reply_text("▶️ <b>Monitoring started!</b>\n\nI'll alert you when I detect motion or people.", parse_mode='HTML')
        else:
            await update.message.reply_text("❌ Failed to start monitoring. Check system status.")
    
    @_require_authorization
    async def cmd_stop_monitoring(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Stop security monitoring."""
        self._update_chat_id(update)
        self.event_manager.stop()
        await update.message.reply_text("⏹️ <b>Monitoring stopped.</b>", parse_mode='HTML')
    
    @_require_authorization
    async def cmd_recordings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show recordings with improved organization by day."""
        self._update_chat_id(update)
        
        recordings = self.event_manager.list_recordings(limit=20)
        
        if not recordings:
            await update.message.reply_text("📂 No recordings found in the last 24 hours.")
            return
        
        # Group recordings by date
        today_recordings = []
        yesterday_recordings = []
        older_recordings = []
        
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        
        for i, rec_path in enumerate(recordings):
            rec_name = Path(rec_path).stem
            
            # Determine event type and icon
            if "person" in rec_name.lower():
                icon = "🚨"
            elif "motion" in rec_name.lower():
                icon = "👀"
            else:
                icon = "📹"
            
            # Extract timestamp and create recording info
            try:
                parts = rec_name.split("_")
                if len(parts) >= 3:
                    timestamp_str = parts[1] + parts[2]
                    dt = datetime.strptime(timestamp_str, "%Y%m%d%H%M%S")
                    time_str = dt.strftime("%H:%M")
                    button_text = f"{icon} {time_str}"
                    
                    recording_info = {
                        'button_text': button_text,
                        'callback_data': f"get_{i}",
                        'datetime': dt
                    }
                    
                    if dt.date() == today:
                        today_recordings.append(recording_info)
                    elif dt.date() == yesterday:
                        yesterday_recordings.append(recording_info)
                    else:
                        older_recordings.append(recording_info)
                else:
                    recording_info = {
                        'button_text': f"{icon} #{i+1}",
                        'callback_data': f"get_{i}",
                        'datetime': datetime.now()
                    }
                    older_recordings.append(recording_info)
            except:
                recording_info = {
                    'button_text': f"{icon} #{i+1}",
                    'callback_data': f"get_{i}",
                    'datetime': datetime.now()
                }
                older_recordings.append(recording_info)
        
        messages_sent = 0
        
        # Send TODAY recordings
        if today_recordings:
            today_buttons = []
            for recording in today_recordings:
                today_buttons.append(InlineKeyboardButton(
                    recording['button_text'], 
                    callback_data=recording['callback_data']
                ))
            
            # Organize in rows of 4
            keyboard = []
            for i in range(0, len(today_buttons), 4):
                keyboard.append(today_buttons[i:i+4])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"📅 <b>Today</b> ({len(today_recordings)} recordings)",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            messages_sent += 1
        
        # Send YESTERDAY recordings
        if yesterday_recordings:
            yesterday_buttons = []
            for recording in yesterday_recordings:
                yesterday_buttons.append(InlineKeyboardButton(
                    recording['button_text'], 
                    callback_data=recording['callback_data']
                ))
            
            # Organize in rows of 4
            keyboard = []
            for i in range(0, len(yesterday_buttons), 4):
                keyboard.append(yesterday_buttons[i:i+4])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"📅 <b>Yesterday</b> ({len(yesterday_recordings)} recordings)",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            messages_sent += 1
        
        # Send OLDER recordings
        if older_recordings:
            # Sort older recordings by date (newest first)
            older_recordings.sort(key=lambda x: x['datetime'], reverse=True)
            
            older_buttons = []
            for recording in older_recordings:
                older_buttons.append(InlineKeyboardButton(
                    recording['button_text'], 
                    callback_data=recording['callback_data']
                ))
            
            # Organize in rows of 4
            keyboard = []
            for i in range(0, len(older_buttons), 4):
                keyboard.append(older_buttons[i:i+4])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Get date range for older recordings
            oldest_date = min(r['datetime'] for r in older_recordings).strftime("%m/%d")
            newest_date = max(r['datetime'] for r in older_recordings).strftime("%m/%d")
            
            date_range = f"{oldest_date}" if oldest_date == newest_date else f"{oldest_date}-{newest_date}"
            
            await update.message.reply_text(
                f"📅 <b>Older</b> ({len(older_recordings)} recordings)\n<i>{date_range}</i>",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            messages_sent += 1
        
        # If no messages were sent, show empty state
        if messages_sent == 0:
            await update.message.reply_text("📂 No recordings available.")
    
    @_require_authorization
    async def cmd_get_recording(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Get specific recording by index or name."""
        self._update_chat_id(update)
        
        if not context.args:
            await update.message.reply_text("Usage: /get <index|event_name>")
            return
        
        recordings = self.event_manager.list_recordings()
        # Sanitize input to prevent path traversal attacks
        key = self._sanitize_input(context.args[0], max_length=200)
        
        if not key:
            await update.message.reply_text("❌ Invalid input.")
            return
        
        try:
            # Try as index first
            idx = int(key)
            if 0 <= idx < len(recordings):
                file_path = recordings[idx]
            else:
                await update.message.reply_text(f"❌ Invalid index. Must be 0-{len(recordings)-1}.")
                return
        except ValueError:
            # Try as event name (already sanitized)
            file_path = self.event_manager.get_recording(key)
            if not file_path:
                await update.message.reply_text("❌ Recording not found.")
                return
        
        # Send the recording
        try:
            if not Path(file_path).exists():
                await update.message.reply_text("❌ Recording file not found.")
                return

            with open(file_path, "rb") as f:
                await update.message.reply_animation(
                    animation=f,
                    caption=f"📹 Recording: {Path(file_path).stem}",
                    read_timeout=30,
                    write_timeout=60
                )

        except (OSError, IOError) as e:
            self.logger.error(f"Error reading recording file: {e}")
            await update.message.reply_text("❌ Failed to read recording file.")
        except (TimedOut, NetworkError) as e:
            self.logger.error(f"Network error sending recording: {e}")
            await update.message.reply_text("❌ Network error sending recording.")
        except TelegramError as e:
            self.logger.error(f"Telegram error sending recording: {e}")
            await update.message.reply_text("❌ Failed to send recording.")

    @_require_authorization
    async def cmd_snapshot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Take and send a live snapshot."""
        self._update_chat_id(update)
        
        try:
            # Check if system is connected
            security_system = self.event_manager.security_system
            if not security_system.capture.is_connected:
                await update.message.reply_text("⚠️ Camera not connected. Trying to connect...")
                if not security_system.capture.connect():
                    await update.message.reply_text("❌ Failed to connect to camera.")
                    return
                await asyncio.sleep(1)  # Allow time to connect
            
            # Capture frame
            frame = security_system.capture.grab()
            if frame is None:
                await update.message.reply_text("❌ Could not capture frame.")
                return
            
            # Validate frame
            if frame.size == 0:
                await update.message.reply_text("❌ Invalid frame captured.")
                return
            
            # Optimize frame size
            height, width = frame.shape[:2]
            if width > 1280:
                scale = 1280 / width
                new_size = (int(width * scale), int(height * scale))
                frame = cv2.resize(frame, new_size)
            
            # Use context manager for temporary file - ensures cleanup
            with _temp_file(Path("temp_snapshot.jpg"), self.logger) as snap_path:
                # Save temporary snapshot
                success = cv2.imwrite(str(snap_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                
                if not success or not snap_path.exists():
                    await update.message.reply_text("❌ Failed to save snapshot.")
                    return
                
                # Read file into memory before deletion
                with open(snap_path, "rb") as f:
                    photo_data = io.BytesIO(f.read())
                    photo_data.name = "snapshot.jpg"  # Set name for proper MIME type
            
            # Send snapshot (file is now in memory, safe to delete temp file)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await update.message.reply_photo(
                photo=photo_data,
                caption=f"📸 <b>Live Snapshot</b>\n📅 {timestamp}",
                parse_mode='HTML',
                read_timeout=30,
                write_timeout=30
            )
            
            self.logger.info("Snapshot sent successfully")
            
        except (TimedOut, NetworkError):
            self.logger.warning("Snapshot send timed out")
            await update.message.reply_text("⚠️ Snapshot sent but may have timed out.")
        except (cv2.error, ValueError) as e:
            self.logger.error(f"OpenCV error: {e}", exc_info=True)
            await update.message.reply_text("❌ Camera processing error.")
        except (OSError, IOError) as e:
            self.logger.error(f"File I/O error: {e}", exc_info=True)
            await update.message.reply_text("❌ Failed to process snapshot file.")
        except Exception as e:
            self.logger.error(f"Unexpected snapshot error: {e}", exc_info=True)
            await update.message.reply_text("❌ Snapshot failed due to unexpected error.")
                    
    @_require_authorization
    async def cmd_show_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show current configuration."""
        self._update_chat_id(update)
        
        security_system = self.event_manager.security_system
        
        config_text = (
            f"⚙️ <b>Current Configuration</b>\n\n"
            
            f"<b>🎥 Camera Settings:</b>\n"
            f"• Source: {security_system.capture.source}\n"
            f"• Interval: {security_system.interval}s\n"
            f"• Record Frames: {security_system.record_frames}\n"
            f"• Confidence: {security_system.confidence}\n"
            f"• Motion Threshold: {security_system.motion_threshold}\n\n"
            
            f"<b>📬 Notification Settings:</b>\n"
            f"• GIF for Motion: {'✅' if self.config.gif_for_motion else '❌'}\n"
            f"• GIF for Person: {'✅' if self.config.gif_for_person else '❌'}\n"
            f"• GIF Duration: {self.config.gif_duration}s\n"
            f"• GIF FPS: {self.config.gif_fps}\n"
            f"• Max File Size: {self.config.max_file_size_mb}MB\n"
            f"• Rate Limit: {self.config.notification_rate_limit}/min"
        )
        
        await update.message.reply_text(config_text, parse_mode='HTML')
    
    @_require_authorization
    async def cmd_configure(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Configure system settings."""
        self._update_chat_id(update)
        
        if len(context.args) < 2:
            help_text = (
                "<b>Usage:</b> /config &lt;key&gt; &lt;value&gt;\n\n"
                "<b>Available Keys:</b>\n"
                "• <code>interval</code> - Check interval (seconds)\n"
                "• <code>frames</code> - Recording frames\n"
                "• <code>confidence</code> - Detection confidence (0.1-0.9)\n"
                "• <code>threshold</code> - Motion threshold\n"
                "• <code>gif_motion</code> - GIF for motion (on/off)\n"
                "• <code>gif_person</code> - GIF for person (on/off)\n"
                "• <code>gif_fps</code> - GIF frame rate (5-30)\n"
                "• <code>max_file_size</code> - Max file size (MB)"
            )
            await update.message.reply_text(help_text, parse_mode='HTML')
            return
        
        # Sanitize inputs
        key = self._sanitize_input(context.args[0].lower(), max_length=50)
        value = self._sanitize_input(context.args[1].lower(), max_length=100)
        
        if not key or not value:
            await update.message.reply_text("❌ Key and value cannot be empty")
            return

        security_system = self.event_manager.security_system
        
        try:
            if key == "interval":
                val_num = float(value)
                if val_num < 0.1:
                    msg = "❌ Interval must be >= 0.1 seconds"
                else:
                    security_system.interval = val_num
                    msg = f"✅ Check interval set to {security_system.interval}s"
                
            elif key == "frames":
                val_num = int(value)
                if val_num < 5:
                    msg = "❌ Frames must be >= 5"
                else:
                    security_system.record_frames = val_num
                    msg = f"✅ Record frames set to {security_system.record_frames}"
                
            elif key == "confidence":
                val_num = float(value)
                if not (0.1 <= val_num <= 0.9):
                    msg = "❌ Confidence must be between 0.1 and 0.9"
                else:
                    security_system.confidence = val_num
                    msg = f"✅ Detection confidence set to {val_num}"
                
            elif key == "threshold":
                val_num = int(value)
                if val_num < 1000:
                    msg = "❌ Threshold must be >= 1000"
                else:
                    security_system.motion_threshold = val_num
                    msg = f"✅ Motion threshold set to {security_system.motion_threshold}"
                
            elif key == "gif_motion":
                self.config.gif_for_motion = value in ['on', 'true', '1', 'yes']
                status = "enabled" if self.config.gif_for_motion else "disabled"
                msg = f"✅ GIF for motion {status}"
                
            elif key == "gif_person":
                self.config.gif_for_person = value in ['on', 'true', '1', 'yes']
                status = "enabled" if self.config.gif_for_person else "disabled"
                msg = f"✅ GIF for person {status}"
                
            elif key == "gif_fps":
                val_num = int(value)
                if not (5 <= val_num <= 30):
                    msg = "❌ GIF FPS must be between 5 and 30"
                else:
                    self.config.gif_fps = val_num
                    msg = f"✅ GIF FPS set to {self.config.gif_fps}"
                
            elif key == "max_file_size":
                val_num = float(value)
                if not (1 <= val_num <= 100):
                    msg = "❌ Max file size must be between 1 and 100 MB"
                else:
                    self.config.max_file_size_mb = val_num
                    msg = f"✅ Max file size set to {self.config.max_file_size_mb}MB"
                
            else:
                msg = "❌ Invalid configuration key"
                
        except ValueError:
            msg = "❌ Invalid value format (check if it should be a number)"
        except Exception as e:
            self.logger.error(f"Configuration error: {e}")
            msg = f"❌ Configuration error: {str(e)[:50]}"
        
        await update.message.reply_text(msg)
    
    @_require_authorization
    async def cmd_force_cleanup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Force cleanup of old files."""
        self._update_chat_id(update)
        
        try:
            files_deleted = self.event_manager.force_cleanup()
            await update.message.reply_text(f"🧹 Cleanup completed: {files_deleted} files deleted")
        except Exception as e:
            self.logger.error(f"Cleanup error: {e}")
            await update.message.reply_text("❌ Cleanup failed")
    
    @_require_authorization
    async def cmd_test_notification(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Test the notification system."""
        self._update_chat_id(update)
        
        test_event = NotificationEvent(
            message="🧪 Test notification",
            event_type="test"
        )
        self._queue_notification(test_event)
        await update.message.reply_text("📨 Test notification queued!")
    
    @_require_authorization
    async def cmd_timeline(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Generate and send timeline plot."""
        self._update_chat_id(update)
        
        # Validate hours argument with helper
        hours = 24  # default
        if context.args:
            validated_hours, error = self._validate_numeric_arg(
                context.args, "hours", min_val=1, max_val=168, as_int=True
            )
            if error:
                await update.message.reply_text(f"{error}\n💡 Using default (24 hours)")
                hours = 24
            else:
                hours = validated_hours
        
        try:
            await update.message.reply_text(f"📊 Generating timeline for last {hours} hours...")
            
            # Generate plot
            plot_data = self.event_manager.get_timeline_plot(hours=hours)
            
            # Send plot
            caption = f"📈 <b>Security Timeline - Last {hours} Hours</b>"
            
            await update.message.reply_photo(
                photo=io.BytesIO(plot_data),
                caption=caption,
                parse_mode='HTML'
            )
            
        except Exception as e:
            self.logger.error(f"Timeline generation failed: {e}")
            await update.message.reply_text("❌ Failed to generate timeline.")

    @_require_authorization
    async def cmd_analytics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show analytics summary."""
        self._update_chat_id(update)
        
        # Validate hours argument with helper
        hours = 24  # default
        if context.args:
            validated_hours, error = self._validate_numeric_arg(
                context.args, "hours", min_val=1, max_val=168, as_int=True
            )
            if error:
                await update.message.reply_text(f"{error}\n💡 Using default (24 hours)")
                hours = 24
            else:
                hours = validated_hours
        
        try:
            stats = self.event_manager.get_analytics_summary(hours=hours)
            
            analytics_text = f"📊 <b>Analytics Summary - Last {hours} Hours</b>\n\n"
            analytics_text += f"<b>Total Events:</b> {stats['total_events']}\n\n"
            
            if stats['by_type']:
                analytics_text += "<b>🔍 By Event Type:</b>\n"
                for event_type, count in sorted(stats['by_type'].items()):
                    icon = {
                        'motion': '👀', 'person': '🚨', 'disconnect': '⚠️',
                        'reconnect': '✅', 'error': '❌', 'startup': '🟢',
                        'shutdown': '🔴', 'storage_warning': '💾'
                    }.get(event_type, '•')
                    analytics_text += f"  {icon} {event_type.title()}: {count}\n"
                analytics_text += "\n"
            
            if stats['by_severity']:
                analytics_text += "<b>⚡ By Severity:</b>\n"
                for severity, count in sorted(stats['by_severity'].items()):
                    icon = {'info': 'ℹ️', 'warning': '⚠️', 'error': '❌'}.get(severity, '•')
                    analytics_text += f"  {icon} {severity.title()}: {count}\n"
                analytics_text += "\n"
            
            if stats['first_event'] and stats['last_event']:
                analytics_text += f"<b>📅 Time Range:</b>\n"
                analytics_text += f"  First: {stats['first_event'].strftime('%m/%d %H:%M')}\n"
                analytics_text += f"  Last: {stats['last_event'].strftime('%m/%d %H:%M')}\n"
            
            await update.message.reply_text(analytics_text, parse_mode='HTML')
            
        except Exception as e:
            self.logger.error(f"Analytics generation failed: {e}")
            await update.message.reply_text("❌ Failed to generate analytics.")
                
    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard button presses."""
        query = update.callback_query

        if not self._is_authorized_user(query.from_user.id):
            await query.answer("🚫 Unauthorized access")
            return

        await query.answer()

        if query.data.startswith("get_"):
            try:
                # Extract and validate recording index
                idx_str = query.data.split("_")[1]
                idx = int(idx_str)
                
                # Validate index range
                if idx < 0:
                    await query.message.reply_text("❌ Invalid recording index")
                    return
                
                recordings = self.event_manager.list_recordings()
                
                if idx >= len(recordings):
                    await query.message.reply_text(f"❌ Recording not found (index: {idx})")
                    return
                
                file_path = recordings[idx]
                
                # Show loading message
                loading_msg = await query.message.reply_text("⏳ Loading recording...")
                
                try:
                    # Validate file exists and is readable
                    if not Path(file_path).exists():
                        await loading_msg.edit_text("❌ Recording file no longer exists")
                        return
                    
                    # Send the recording
                    with open(file_path, "rb") as f:
                        await query.message.reply_animation(
                            animation=f,
                            caption=f"📹 {Path(file_path).stem}",
                            read_timeout=30,
                            write_timeout=60
                        )
                    await loading_msg.delete()
                    
                except (OSError, IOError) as e:
                    await loading_msg.edit_text("❌ Failed to read recording file")
                    self.logger.error(f"File I/O error in callback: {e}")
                except Exception as e:
                    await loading_msg.edit_text("❌ Failed to send recording")
                    self.logger.error(f"Error sending recording via callback: {e}")
                    
            except (ValueError, IndexError) as e:
                self.logger.warning(f"Invalid callback data: {query.data}")
                await query.message.reply_text("❌ Invalid request format")
            except Exception as e:
                self.logger.error(f"Callback query error: {e}", exc_info=True)
                await query.message.reply_text("❌ Error processing request")
    
    async def start_bot(self) -> None:
        """Start the Telegram bot."""
        try:
            self.logger.info("Starting Telegram bot...")
            
            # Initialize application
            await self.app.initialize()
            await self.app.start()
            
            # Start notification processor
            self.app.job_queue.run_repeating(
                self._process_notification_queue,
                interval=1.0,
                first=0.5
            )
            
            # Start polling
            await self.app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES
            )
            
            self.logger.info("Telegram bot started successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to start bot: {e}", exc_info=True)
            raise
    
    async def stop_bot(self) -> None:
        """Stop the Telegram bot gracefully."""
        try:
            self.logger.info("Stopping Telegram bot...")
            
            # Stop polling
            if self.app.updater.running:
                await self.app.updater.stop()
            
            # Stop application
            await self.app.stop()
            await self.app.shutdown()
            
            self.logger.info("Telegram bot stopped successfully")
            
        except Exception as e:
            self.logger.error(f"Error stopping bot: {e}", exc_info=True)
    
    async def run_forever(self) -> None:
        """Run the bot until interrupted."""
        try:
            await self.start_bot()
            
            self.logger.info("Bot running. Press Ctrl+C to stop...")
            
            # Wait indefinitely
            stop_event = asyncio.Event()
            
            def signal_handler():
                self.logger.info("Shutdown signal received")
                stop_event.set()
            
            # Handle shutdown gracefully
            try:
                await stop_event.wait()
            except KeyboardInterrupt:
                self.logger.info("Keyboard interrupt received")
            
        finally:
            await self.stop_bot()
    
    def run(self) -> None:
        """Synchronous wrapper to run the bot."""
        try:
            asyncio.run(self.run_forever())
        except KeyboardInterrupt:
            self.logger.info("Bot stopped by user")
        except Exception as e:
            self.logger.error(f"Bot crashed: {e}", exc_info=True)
            raise
    
    @property
    def status(self) -> Dict[str, Any]:
        """Get current bot status and statistics."""
        return {
            'chat_id': self.chat_id,
            'queue_size': self.notification_queue.qsize(),
            'notifications_sent': self.notification_stats['sent'],
            'notifications_failed': self.notification_stats['failed'],
            'rate_limited_count': self.notification_stats['rate_limited'],
            'last_notification': self.notification_stats['last_notification'],
            'config': asdict(self.config),
            'event_manager_running': self.event_manager.is_running
        }
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        # Note: async cleanup would need to be handled differently
        pass
    
    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"SecurityTelegramBot(chat_id={self.chat_id}, notifications_sent={self.notification_stats['sent']})"