"""
Core infrastructure for the modular Spyoncino architecture.

This package currently exposes the asynchronous event bus, contracts, and
the orchestrator skeleton required for the first integration step.
"""

from .bus import EventBus, Subscription
from .contracts import (
    BaseModule,
    BasePayload,
    DetectionEvent,
    Frame,
    HealthStatus,
    ModuleConfig,
)
from .orchestrator import Orchestrator

__all__ = [
    "BaseModule",
    "BasePayload",
    "DetectionEvent",
    "EventBus",
    "Frame",
    "HealthStatus",
    "ModuleConfig",
    "Orchestrator",
    "Subscription",
]
