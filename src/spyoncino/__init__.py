"""
Spyoncino - AI-powered security system

A surveillance system with motion detection, person recognition,
and Telegram notifications.
"""

__version__ = "0.0.1-alpha"

from spyoncino.capture import Capture
from spyoncino.security import SecuritySystem
from spyoncino.manager import SecurityEventManager
from spyoncino.bot import SecurityTelegramBot, BotConfig
from spyoncino.analytics import EventLogger, SecurityEvent, EventType

__all__ = [
    "Capture",
    "SecuritySystem",
    "SecurityEventManager",
    "SecurityTelegramBot",
    "BotConfig",
    "EventLogger",
    "SecurityEvent",
    "EventType",
]

