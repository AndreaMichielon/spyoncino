"""
Compatibility wrapper for invoking the legacy CLI entrypoint.

New orchestration logic will eventually live in the modular core, but for
now this simply forwards all behavior to `spyoncino.legacy.run`.
"""

from .legacy.run import *  # noqa: F403
