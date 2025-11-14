"""
Legacy monolithic implementation of the Spyoncino security system.

These modules are kept under the `spyoncino.legacy` namespace while the
codebase transitions to the modular architecture described in
`TODO_ARCHITECTURE.md`.
"""

from .analytics import EventLogger, EventType, SecurityEvent
from .bot import BotConfig, SecurityTelegramBot
from .capture import Capture
from .manager import SecurityEventManager
from .security import SecuritySystem

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
