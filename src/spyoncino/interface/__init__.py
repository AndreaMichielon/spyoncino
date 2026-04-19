"""
Interface module - Notification and access interfaces.

Provides:
- TelegramBotInterface: Telegram bot for event notifications
- WebAppInterface: FastAPI web application for metrics and configuration
- MemoryManager: Permanent storage for metrics, events, and configuration
"""

from __future__ import annotations

from .memory_manager import (
    MemoryManager,
    EventType,
    Event,
    ServiceStatus,
    SystemMetrics,
)
from .telegram_bot import TelegramBotInterface, NotificationEvent

__all__ = [
    "MemoryManager",
    "EventType",
    "Event",
    "ServiceStatus",
    "SystemMetrics",
    "TelegramBotInterface",
    "NotificationEvent",
    "WebAppInterface",
]


def __getattr__(name: str):
    if name == "WebAppInterface":
        from .webapp import WebAppInterface

        return WebAppInterface
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
