"""
Spyoncino - AI-powered security system

A surveillance system with motion detection, person recognition,
and Telegram notifications.
"""

__version__ = "0.0.1-alpha"

from spyoncino.analytics import EventLogger, EventType, SecurityEvent
from spyoncino.bot import BotConfig, SecurityTelegramBot
from spyoncino.capture import Capture
from spyoncino.manager import SecurityEventManager
from spyoncino.security import SecuritySystem

__all__ = [
    "BotConfig",
    "Capture",
    "EventLogger",
    "EventType",
    "SecurityEvent",
    "SecurityEventManager",
    "SecuritySystem",
    "SecurityTelegramBot",
]
