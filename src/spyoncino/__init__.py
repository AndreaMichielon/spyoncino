"""
Spyoncino - AI-powered surveillance system.

Main components:
- Orchestrator: Main loop for coordinating components
- Input: Camera input sources
- Preprocessing: Motion detection
- Inference: Object detection
- Interface: Notification and access interfaces
"""

from __future__ import annotations

__all__ = ["Orchestrator"]


def __getattr__(name: str):
    if name == "Orchestrator":
        from .orchestrator import Orchestrator

        return Orchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
