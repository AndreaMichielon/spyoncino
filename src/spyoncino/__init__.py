"""
Spyoncino - AI-powered security system

A surveillance system with motion detection, person recognition,
and Telegram notifications.
"""

__version__ = "0.0.1-alpha"

from spyoncino.legacy import (
    BotConfig,
    Capture,
    EventLogger,
    EventType,
    SecurityEvent,
    SecurityEventManager,
    SecuritySystem,
    SecurityTelegramBot,
)

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
